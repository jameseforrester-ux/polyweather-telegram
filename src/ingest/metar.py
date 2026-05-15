"""METAR ingestor.

Pulls latest METAR observations from aviationweather.gov for each airport
we care about, and reports the max observed temperature so far in the
airport's local calendar day (which is what Polymarket markets resolve on).
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
import httpx

from .. import db
from .climatology import Airport, now_local

log = logging.getLogger(__name__)

METAR_URL = "https://aviationweather.gov/api/data/metar"


async def fetch_metars(icao: str, hours: int = 30) -> list[dict]:
    """Returns raw METAR JSON list for the given ICAO, most-recent first."""
    params = {"ids": icao, "format": "json", "taf": "false", "hours": str(hours)}
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(METAR_URL, params=params, headers={"User-Agent": "polywx-bot/1.0"})
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            return []
        return data


def _temp_f_from_metar(m: dict) -> float | None:
    t = m.get("temp")
    if t is None:
        return None
    try:
        return float(t) * 9.0 / 5.0 + 32.0
    except (TypeError, ValueError):
        return None


def _obs_time_utc(m: dict) -> datetime | None:
    ts = m.get("obsTime")
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


async def ingest_airport(icao: str) -> int:
    """Fetch + persist METARs for one airport. Returns count of new rows."""
    try:
        raw = await fetch_metars(icao, hours=30)
    except Exception as e:
        log.warning("METAR fetch failed for %s: %s", icao, e)
        return 0
    n = 0
    for m in raw:
        ot = _obs_time_utc(m)
        tf = _temp_f_from_metar(m)
        if ot is None or tf is None:
            continue
        db.upsert_metar(icao, ot.isoformat(timespec="seconds"), tf)
        n += 1
    log.info("METAR %s: %d obs persisted", icao, n)
    return n


def today_max_obs_f(airport: Airport) -> tuple[float | None, datetime | None]:
    """Return (max_temp_f_today_so_far, obs_time_of_that_max_utc).

    "Today" = airport-local calendar day, from local midnight up to now.
    """
    n_local = now_local(airport)
    start_local = n_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc).isoformat(timespec="seconds")
    obs = db.metars_since(airport.icao, start_utc)
    if not obs:
        return None, None
    best_t, best_time = None, None
    for ot_str, tf in obs:
        if tf is None:
            continue
        if best_t is None or tf > best_t:
            best_t = tf
            best_time = datetime.fromisoformat(ot_str)
    return best_t, best_time
