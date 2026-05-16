"""NWP dispatch: HRRR for CONUS (K-prefix ICAOs), GFS for everything else."""
from __future__ import annotations
import logging
from .climatology import Airport
from . import hrrr as _hrrr
from . import gfs as _gfs

log = logging.getLogger(__name__)


def is_conus_icao(icao: str) -> bool:
    """K-prefix = CONUS airport. (Alaska is P*, Hawaii is PH*, both outside HRRR.)"""
    return bool(icao) and icao.upper().startswith("K") and len(icao) == 4


def ingest_airport(airport: Airport):
    """Pick the right model and ingest forecast for this airport."""
    if is_conus_icao(airport.icao):
        return _hrrr.ingest_airport(airport)
    return _gfs.ingest_airport(airport)
