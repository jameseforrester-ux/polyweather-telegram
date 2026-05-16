"""HRRR/GFS forecasts via Open-Meteo API.

Replaces the old Herbie/GRIB-based fetcher. Pure HTTPS JSON — no native deps,
no eccodes, no cfgrib, no segfaults.

For CONUS airports we ask Open-Meteo to use the `gfs_hrrr` model (HRRR at
3 km when available, GFS fallback after HRRR's 18-48h horizon). Multi-day
forecasts are returned in a single request, so for each ingest cycle we
populate predictions for every upcoming target date the airport has an
active event for.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import datetime, timezone

import httpx

from .. import db
from .climatology import Airport, now_local

log = logging.getLogger(__name__)

URL = "https://api.open-meteo.com/v1/forecast"


def _target_dates_for(airport: Airport) -> list[str]:
    """Local dates this airport has active events for."""
    seen = []
    for ev in db.list_active_events():
        if ev["airport_icao"] == airport.icao and ev["local_date"]:
            if ev["local_date"] not in seen:
                seen.append(ev["local_date"])
    return sorted(seen)


async def fetch_daily_maxes(airport: Airport, models: str = "gfs_hrrr") -> dict[str, float]:
    """Returns {local_date: max_temp_F} for every available forecast day.
    Open-Meteo returns up to ~7 days. The `daily=temperature_2m_max` parameter
    aggregates hourly temps at airport-local midnight boundaries, which is
    exactly what Polymarket resolves on."""
    params = {
        "latitude": str(airport.lat),
        "longitude": str(airport.lon),
        "daily": "temperature_2m_max",
        "temperature_unit": "fahrenheit",
        "timezone": airport.tz,
        "forecast_days": "7",
        "models": models,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(URL, params=params,
                            headers={"User-Agent": "polywx-bot/1.0"})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.warning("Open-Meteo fetch failed for %s: %s", airport.icao, e)
        return {}

    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    maxes = daily.get("temperature_2m_max") or []
    out: dict[str, float] = {}
    for d, t in zip(dates, maxes):
        if t is not None:
            out[d] = float(t)
    return out


async def ingest_airport(airport: Airport) -> tuple[int, dict[str, float]]:
    """Fetch + persist daily-max forecasts for all upcoming dates this airport
    has active events for. Returns (n_dates_written, {date: pred_f})."""
    targets = _target_dates_for(airport)
    forecasts = await fetch_daily_maxes(airport, models="gfs_hrrr")
    if not forecasts:
        log.info("HRRR %s: no forecast data returned", airport.icao)
        return 0, {}

    init_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for target in targets:
        pred = forecasts.get(target)
        if pred is not None:
            db.upsert_hrrr_pred(airport.icao, init_str, target, pred)
            n += 1
    msg_targets = ", ".join(
        f"{d}={forecasts[d]:.1f}°F" for d in targets if d in forecasts
    )
    log.info("HRRR %s: %d dates written (%s)", airport.icao, n, msg_targets or "no overlap")
    return n, {d: forecasts[d] for d in targets if d in forecasts}
