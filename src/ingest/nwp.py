"""NWP dispatch: HRRR for CONUS (K-prefix ICAOs), GFS for everything else.
Both routes use Open-Meteo under the hood (no native deps)."""
from __future__ import annotations
from .climatology import Airport
from . import hrrr as _hrrr
from . import gfs as _gfs


def is_conus_icao(icao: str) -> bool:
    return bool(icao) and icao.upper().startswith("K") and len(icao) == 4


async def ingest_airport(airport: Airport):
    if is_conus_icao(airport.icao):
        return await _hrrr.ingest_airport(airport)
    return await _gfs.ingest_airport(airport)
