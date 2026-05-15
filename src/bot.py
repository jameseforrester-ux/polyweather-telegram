"""Telegram bot handlers.

Owner-only (single user) — the bot ignores messages from any chat_id other
than TELEGRAM_OWNER_CHAT_ID.

Commands:
  /start       welcome + main menu
  /markets     list active high-temp markets (inline buttons)
  /fair <city> show fair distribution + edge for a city (matches by keyword)
  /watches     list pinned buckets, with unpin buttons
  /status      data freshness snapshot

Callback data scheme:
  ev:<event_id>           -> show event detail
  back                    -> back to markets
  watch:<market_id>       -> pin a bucket
  unwatch:<market_id>     -> unpin
  refresh:<event_id>      -> recompute and re-display event detail
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

from . import db
from .analytics.edge import compute_event_view, EventView, BucketView
from .ingest.climatology import get_airport, now_local

log = logging.getLogger(__name__)

OWNER_CHAT_ID = int(os.getenv("TELEGRAM_OWNER_CHAT_ID", "0"))


# ---------------------------------------------------------------------------
# Auth gate

def _is_owner(update: Update) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    if OWNER_CHAT_ID == 0:
        # Not configured — refuse everyone, log loudly
        log.warning("TELEGRAM_OWNER_CHAT_ID not set; ignoring %s", chat.id)
        return False
    return chat.id == OWNER_CHAT_ID


async def _deny(update: Update):
    if update.callback_query:
        await update.callback_query.answer("Not authorized.", show_alert=True)
    elif update.message:
        await update.message.reply_text("Not authorized.")


# ---------------------------------------------------------------------------
# Formatting

def _fmt_temp(v: float | None, suffix: str = "°F") -> str:
    return "—" if v is None else f"{v:.1f}{suffix}"


def _fmt_event_view(view: EventView) -> str:
    lines = [
        f"*{view.title}*",
        f"Resolves: `{view.airport_icao}`  ·  Local date: `{view.local_date}`",
        "",
        f"Obs max so far: *{_fmt_temp(view.obs_max_f)}*",
        f"HRRR latest run: {_fmt_temp(view.hrrr_pred_max_f)}",
        f"Fair max (blended): *{_fmt_temp(view.fair_max_f)}*  ±{view.sigma_f:.1f}°F",
        "",
        "`bucket          fair    ask    edge`",
    ]
    for b in view.buckets:
        ask_str = "  —  " if b.best_ask is None else f"{b.best_ask*100:5.1f}¢"
        lines.append(
            f"`{b.label:<14s} {b.fair_prob*100:5.1f}%  {ask_str}  {b.edge_str():>7s}`"
        )
    lines.append("")
    n = now_local(get_airport(view.airport_icao)) if get_airport(view.airport_icao) else None
    if n:
        lines.append(f"_Local time at airport: {n.strftime('%Y-%m-%d %H:%M %Z')}_")
    return "\n".join(lines)


def _event_keyboard(view: EventView) -> InlineKeyboardMarkup:
    # One row per bucket with Watch/Unwatch toggle; bottom row Refresh/Back
    watched = {w["market_id"] for w in db.list_watches()}
    rows = []
    for b in view.buckets:
        is_watched = b.market_id in watched
        rows.append([
            InlineKeyboardButton(
                ("👁  " if is_watched else "    ") + f"{b.label}",
                callback_data="noop",
            ),
            InlineKeyboardButton(
                "Unwatch" if is_watched else "Watch",
                callback_data=("unwatch:" if is_watched else "watch:") + b.market_id,
            ),
        ])
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{view.event_id}"),
        InlineKeyboardButton("« Markets", callback_data="back"),
    ])
    return InlineKeyboardMarkup(rows)


def _markets_keyboard(events: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for ev in events[:25]:  # cap for sanity
        label = f"{ev['airport_icao'] or '???'}  ·  {ev['local_date']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ev:{ev['event_id']}")])
    if not rows:
        rows.append([InlineKeyboardButton("(no active markets yet)", callback_data="noop")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Command handlers

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    text = (
        "*PolyWX bot online.*\n\n"
        "Commands:\n"
        "  /markets — list active high-temp markets\n"
        "  /fair <city> — fair value for a city (e.g. `/fair nyc`)\n"
        "  /watches — your pinned buckets\n"
        "  /status — data freshness"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    events = db.list_active_events()
    text = f"*Active markets ({len(events)})* — tap to drill in:"
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=_markets_keyboard(events)
    )


async def cmd_fair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: `/fair <city>`", parse_mode=ParseMode.MARKDOWN)
        return
    q = " ".join(context.args)
    hits = db.find_event_by_keyword(q)
    if not hits:
        await update.message.reply_text(f"No active market matches `{q}`.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    view = await compute_event_view(hits[0]["event_id"])
    if view is None:
        await update.message.reply_text("Failed to compute view.")
        return
    await update.message.reply_text(
        _fmt_event_view(view), parse_mode=ParseMode.MARKDOWN,
        reply_markup=_event_keyboard(view),
    )


async def cmd_watches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    ws = db.list_watches()
    if not ws:
        await update.message.reply_text("No pinned buckets.")
        return
    # Look up bucket labels
    rows = []
    lines = ["*Pinned buckets:*"]
    for w in ws:
        with db.db() as c:
            r = c.execute(
                """SELECT b.label, m.title, m.event_id, m.airport_icao
                   FROM buckets b JOIN markets m ON b.event_id = m.event_id
                   WHERE b.market_id = ?""", (w["market_id"],)
            ).fetchone()
        if r is None:
            continue
        lines.append(f"• {r['airport_icao']} — {r['label']}")
        rows.append([
            InlineKeyboardButton(f"Open {r['airport_icao']}", callback_data=f"ev:{r['event_id']}"),
            InlineKeyboardButton("Unpin", callback_data=f"unwatch:{w['market_id']}"),
        ])
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    events = db.list_active_events()
    icaos = sorted({e["airport_icao"] for e in events if e["airport_icao"]})
    lines = [f"*Status* — {len(events)} active events"]
    with db.db() as c:
        for icao in icaos:
            r = c.execute(
                "SELECT MAX(obs_time_utc) AS t FROM metar_obs WHERE icao = ?", (icao,)
            ).fetchone()
            h = c.execute(
                """SELECT MAX(fetched_utc) AS t, MAX(run_init_utc) AS init
                   FROM hrrr_runs WHERE icao = ?""", (icao,)
            ).fetchone()
            lines.append(
                f"`{icao}`  METAR: {r['t'] or '—'}\n"
                f"        HRRR run: {h['init'] or '—'}  (fetched {h['t'] or '—'})"
            )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Callback handler

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "noop":
        return

    if data == "back":
        events = db.list_active_events()
        text = f"*Active markets ({len(events)})* — tap to drill in:"
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=_markets_keyboard(events))
        return

    if data.startswith("ev:") or data.startswith("refresh:"):
        event_id = data.split(":", 1)[1]
        view = await compute_event_view(event_id)
        if view is None:
            await q.edit_message_text("Failed to compute view.")
            return
        await q.edit_message_text(
            _fmt_event_view(view), parse_mode=ParseMode.MARKDOWN,
            reply_markup=_event_keyboard(view),
        )
        return

    if data.startswith("watch:"):
        market_id = data.split(":", 1)[1]
        db.add_watch(market_id)
        # Re-render the event detail this bucket belongs to
        with db.db() as c:
            r = c.execute("SELECT event_id FROM buckets WHERE market_id = ?",
                          (market_id,)).fetchone()
        if r:
            view = await compute_event_view(r["event_id"])
            if view:
                await q.edit_message_text(
                    _fmt_event_view(view), parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_event_keyboard(view),
                )
        return

    if data.startswith("unwatch:"):
        market_id = data.split(":", 1)[1]
        db.remove_watch(market_id)
        with db.db() as c:
            r = c.execute("SELECT event_id FROM buckets WHERE market_id = ?",
                          (market_id,)).fetchone()
        if r:
            view = await compute_event_view(r["event_id"])
            if view:
                await q.edit_message_text(
                    _fmt_event_view(view), parse_mode=ParseMode.MARKDOWN,
                    reply_markup=_event_keyboard(view),
                )
        return


# ---------------------------------------------------------------------------
# Wiring

def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("markets", cmd_markets))
    app.add_handler(CommandHandler("fair", cmd_fair))
    app.add_handler(CommandHandler("watches", cmd_watches))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
