"""International forecasts via Open-Meteo API.

Same shape as hrrr.py but uses `models=best_match` so Open-Meteo picks the
highest-resolution regional model available (ICON-D2 for Europe, ECMWF
elsewhere, GFS as global fallback)."""
from __future__ import annotations
import logging
from .climatology import Airport
from . import hrrr as _hrrr  # reuse fetch_daily_maxes signature

log = logging.getLogger(__name__)


async def ingest_airport(airport: Airport):
    targets = _hrrr._target_dates_for(airport)
    forecasts = await _hrrr.fetch_daily_maxes(airport, models="best_match")
    if not forecasts:
        log.info("GFS %s: no forecast data returned", airport.icao)
        return 0, {}

    from datetime import datetime, timezone
    from .. import db
    init_str = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    for target in targets:
        pred = forecasts.get(target)
        if pred is not None:
            db.upsert_hrrr_pred(airport.icao, init_str, target, pred)
            n += 1
    msg = ", ".join(f"{d}={forecasts[d]:.1f}°F" for d in targets if d in forecasts)
    log.info("GFS %s: %d dates written (%s)", airport.icao, n, msg or "no overlap")
    return n, {d: forecasts[d] for d in targets if d in forecasts}
