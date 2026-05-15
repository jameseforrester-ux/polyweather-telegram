"""HRRR ingestor via Herbie.

For a given airport, finds the most recent HRRR run that's available, pulls
the 2 m temperature for each forecast hour that covers the rest of the local
day, and returns the max in °F.

HRRR usually posts to AWS ~50 minutes after the init hour. Herbie tries
multiple sources (NOMADS, AWS, Google) and picks the first that has the file.
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
    """The most recent HRRR init hour likely to have posted to AWS."""
    now = datetime.now(timezone.utc)
    # HRRR posts ~50 min after the top of its init hour; be conservative.
    if now.minute >= 55:
        return now.replace(minute=0, second=0, microsecond=0)
    return (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def _is_long_run(init: datetime) -> bool:
    """The 00/06/12/18Z runs go out to F48. Hourly runs go to F18."""
    return init.hour in (0, 6, 12, 18)


def _nearest_grid_value(ds, lat: float, lon: float) -> float:
    """Find the value at the grid point closest to (lat, lon)."""
    lats = ds.latitude.values
    lons = ds.longitude.values
    # HRRR lons are 0..360 east; convert input to same convention if needed.
    lon_e = lon % 360
    cos_lat = math.cos(math.radians(lat))
    dlat = lats - lat
    dlon = (lons - lon_e + 180) % 360 - 180
    d2 = dlat * dlat + (dlon * cos_lat) ** 2
    iy, ix = np.unravel_index(np.argmin(d2), d2.shape)
    # variable name varies; pick the first data var
    var_name = next(iter(ds.data_vars))
    return float(ds[var_name].values[iy, ix])


def _fetch_one_hour(init_utc: datetime, fxx: int, airport: Airport) -> float | None:
    """Return 2m temp °F at airport grid point for forecast hour fxx of init_utc."""
    from herbie import Herbie  # imported lazily so module import doesn't pull herbie until needed

    try:
        H = Herbie(
            init_utc.strftime("%Y-%m-%d %H:00"),
            model="hrrr",
            product="sfc",
            fxx=fxx,
            verbose=False,
        )
        ds = H.xarray(":TMP:2 m above ground:")
    except Exception as e:
        log.debug("Herbie miss for %s F%02d: %s", init_utc, fxx, e)
        return None

    try:
        t_k = _nearest_grid_value(ds, airport.lat, airport.lon)
    except Exception as e:
        log.warning("Grid extract failed %s F%02d %s: %s", init_utc, fxx, airport.icao, e)
        return None

    return t_k * 9.0 / 5.0 - 459.67  # K -> °F


def fetch_today_max(airport: Airport) -> tuple[datetime, float] | tuple[None, None]:
    """Pull HRRR forecasts that cover the remainder of `airport`'s local day and
    return (run_init_utc, max_temp_f). If peak heating is already past, returns
    (run_init_utc, None) so the caller can fall back to the observed max."""
    init = _latest_likely_init_utc()
    start_utc, end_utc = local_today_bounds_utc(airport)
    now_utc = datetime.now(timezone.utc)
    coverage_start = max(now_utc, init + timedelta(hours=1))
    coverage_end = end_utc

    if coverage_start >= coverage_end:
        return init, None

    max_fxx = 48 if _is_long_run(init) else 18

    temps: list[float] = []
    for fxx in range(1, max_fxx + 1):
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


def ingest_airport(airport: Airport) -> tuple[datetime | None, float | None]:
    """Persist the latest-run max forecast for the airport's local date."""
    init, pred_max = fetch_today_max(airport)
    if init is None:
        return None, None
    local_date = now_local(airport).date().isoformat()
    db.upsert_hrrr_pred(
        airport.icao,
        init.isoformat(timespec="seconds"),
        local_date,
        pred_max,
    )
    log.info("HRRR %s @ %s for %s: pred_max=%s", airport.icao,
             init.strftime("%Y-%m-%dT%H"), local_date,
             "n/a" if pred_max is None else f"{pred_max:.1f}°F")
    return init, pred_max
