"""Job functions invoked by the bot's JobQueue.

The bot's `application.job_queue` is a thin wrapper over APScheduler; we
register these as run_repeating jobs in main.py. Each job receives a
CallbackContext and can use `context.bot` to push messages.
"""
from __future__ import annotations
import asyncio
import logging
import os

from telegram.ext import ContextTypes

from .db import init_schema, list_active_events
from .ingest import polymarket as pm
from .ingest import metar as metar_mod
from .ingest import nwp as nwp_mod
from .ingest.climatology import load_airports, get_airport
from .alerts.triggers import evaluate_all, Alert

log = logging.getLogger(__name__)

OWNER_CHAT_ID = int(os.getenv("TELEGRAM_OWNER_CHAT_ID", "0"))


def _format_alert(a: Alert) -> str:
    icon = {
        "LEADER_DROP": "📉",
        "WATCH_DROP": "📉",
        "BUSTED": "🚨",
        "FLOOR_UNREACHABLE": "🚨",
    }.get(a.kind, "ℹ️")
    return f"{icon} *{a.kind}*\n{a.title}\n_{a.bucket_label}_\n{a.detail}"


# ---------------------------------------------------------------------------
# Jobs

async def job_polymarket(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await pm.ingest_events()
    except Exception as e:
        log.exception("polymarket ingest error: %s", e)


async def job_metar(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Ingest only airports that have an active event (saves API calls)
    active_icaos = {
        ev["airport_icao"] for ev in list_active_events() if ev["airport_icao"]
    }
    if not active_icaos:
        # warm cache with all known airports anyway, so /fair works pre-event-load
        active_icaos = set(load_airports().keys())
    for icao in active_icaos:
        try:
            await metar_mod.ingest_airport(icao)
        except Exception as e:
            log.exception("metar ingest %s error: %s", icao, e)


async def job_hrrr(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hourly NWP refresh. Calls Open-Meteo for each active airport.
    Sequential to be polite to the free tier; each call ~1s."""
    active_icaos = sorted({
        ev["airport_icao"] for ev in list_active_events() if ev["airport_icao"]
    })
    if not active_icaos:
        return
    for icao in active_icaos:
        ap = get_airport(icao)
        if ap is None:
            continue
        try:
            await nwp_mod.ingest_airport(ap)
        except Exception as e:
            log.exception("NWP ingest %s error: %s", icao, e)


async def job_alerts(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        alerts = await evaluate_all()
    except Exception as e:
        log.exception("alert evaluation error: %s", e)
        return
    if not alerts:
        return
    if not OWNER_CHAT_ID:
        log.warning("Alerts produced but TELEGRAM_OWNER_CHAT_ID not set; "
                    "%d alert(s) discarded.", len(alerts))
        return
    for a in alerts:
        try:
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=_format_alert(a),
                parse_mode="Markdown",
            )
        except Exception as e:
            log.exception("alert send failed: %s", e)


def ensure_db():
    init_schema()
