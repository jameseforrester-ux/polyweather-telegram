"""GFS (Global Forecast System) ingestor via Herbie.

Used for any airport HRRR can't cover — i.e., non-CONUS. GFS runs every
6 hours (00/06/12/18Z), 0.25° resolution (~28 km grid), forecasts out to
384 hours. We pull forecast hours covering up to the resolution airport's
local midnight, find the 2 m temp at the nearest grid point, take the max.

Less sharp than HRRR (3 km vs 28 km), and updates every 6h instead of 1h,
but covers every airport on Earth. Accuracy class: comparable to ECMWF
Open Data 0.25°. For Polymarket's international markets that's the best
available free option.
"""
from __future__ import annotations
import logging
import math
from datetime import datetime, timedelta, timezone

import numpy as np

from .. import db
from .climatology import Airport, now_local, local_today_bounds_utc

log = logging.getLogger(__name__)


def _latest_likely_init_utc() -> datetime:
    """Most recent GFS init likely posted. GFS runs at 00/06/12/18Z; data
    typically posts to NOMADS/AWS ~4-5 hours after init for full coverage."""
    now = datetime.now(timezone.utc)
    # Find the latest 6-hourly init that's >= 5 hours old
    for hours_back in range(0, 24):
        candidate = (now - timedelta(hours=hours_back)).replace(minute=0, second=0, microsecond=0)
        if candidate.hour % 6 == 0 and (now - candidate) >= timedelta(hours=5):
            return candidate
    # Fallback: 24h ago, rounded to last 6h init
    fallback = (now - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
    return fallback.replace(hour=(fallback.hour // 6) * 6)


def _nearest_grid_value(ds, lat: float, lon: float) -> float:
    lats = ds.latitude.values
    lons = ds.longitude.values
    lon_e = lon % 360
    cos_lat = math.cos(math.radians(lat))
    # GFS lat/lon are usually 1-D arrays (regular grid)
    if lats.ndim == 1 and lons.ndim == 1:
        iy = int(np.argmin(np.abs(lats - lat)))
        # handle longitude wrap
        dlon = (lons - lon_e + 180) % 360 - 180
        ix = int(np.argmin(np.abs(dlon)))
    else:
        dlat = lats - lat
        dlon = (lons - lon_e + 180) % 360 - 180
        d2 = dlat * dlat + (dlon * cos_lat) ** 2
        iy, ix = np.unravel_index(np.argmin(d2), d2.shape)
    var_name = next(iter(ds.data_vars))
    val = ds[var_name].values
    return float(val[iy, ix]) if val.ndim == 2 else float(val[..., iy, ix].squeeze())


def _fetch_one_hour(init_utc: datetime, fxx: int, airport: Airport) -> float | None:
    from herbie import Herbie
    try:
        H = Herbie(
            init_utc.strftime("%Y-%m-%d %H:00"),
            model="gfs",
            product="pgrb2.0p25",
            fxx=fxx,
            verbose=False,
        )
        ds = H.xarray(":TMP:2 m above ground:")
    except Exception as e:
        log.debug("Herbie GFS miss for %s F%03d: %s", init_utc, fxx, e)
        return None
    try:
        t_k = _nearest_grid_value(ds, airport.lat, airport.lon)
    except Exception as e:
        log.warning("GFS grid extract failed %s F%03d %s: %s",
                    init_utc, fxx, airport.icao, e)
        return None
    return t_k * 9.0 / 5.0 - 459.67


def fetch_today_max(airport: Airport):
    """Returns (run_init_utc, max_temp_f) covering remainder of airport-local day."""
    init = _latest_likely_init_utc()
    start_utc, end_utc = local_today_bounds_utc(airport)
    now_utc = datetime.now(timezone.utc)
    coverage_start = max(now_utc, init + timedelta(hours=1))
    coverage_end = end_utc
    if coverage_start >= coverage_end:
        return init, None

    # GFS posts hourly out to F120, then every 3h beyond. For day-of/next-day
    # forecasts we're always in the hourly range.
    temps: list[float] = []
    for fxx in range(1, 49):
        valid = init + timedelta(hours=fxx)
        if valid < coverage_start:
            continue
        if valid > coverage_end:
            break
        t_f = _fetch_one_hour(init, fxx, airport)
        if t_f is not None:
            temps.append(t_f)
    if not temps:
        return init, None
    return init, max(temps)


def ingest_airport(airport: Airport):
    init, pred_max = fetch_today_max(airport)
    if init is None:
        return None, None
    local_date = now_local(airport).date().isoformat()
    db.upsert_hrrr_pred(  # reuse same table; "hrrr" name is historical
        airport.icao,
        init.isoformat(timespec="seconds"),
        local_date,
        pred_max,
    )
    log.info("GFS %s @ %s for %s: pred_max=%s", airport.icao,
             init.strftime("%Y-%m-%dT%H"), local_date,
             "n/a" if pred_max is None else f"{pred_max:.1f}°F")
    return init, pred_max
