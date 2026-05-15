"""Airport metadata + diurnal climatology.

Design (revised after discovering Polymarket uses non-obvious resolution
airports like KBKF for Denver and KHOU for Houston):

- The bot identifies the resolution airport from the Wunderground URL in
  each market's description (handled in ingest/polymarket.py).
- Airport metadata (lat, lon, timezone, name) comes from the `airportsdata`
  package — covers every ICAO in the world (~28k airports), so we never
  have to maintain a per-airport coordinate table.
- Monthly climatological normals come from `config/airport_normals.yaml`,
  hand-curated only for the airports Polymarket actually uses.
- When Polymarket adds a city we haven't curated, the bot falls back to a
  parametric latitude × month climatology so the market still works
  (degraded accuracy). A warning is logged so the operator knows to add
  real normals.
"""
from __future__ import annotations
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

import airportsdata
import yaml

log = logging.getLogger(__name__)

NORMALS_PATH = Path(__file__).parent.parent.parent / "config" / "airport_normals.yaml"
SUNRISE_HOUR = 6.0


@dataclass
class Airport:
    icao: str
    name: str
    iata: str | None
    lat: float
    lon: float
    tz: str
    peak_local_hour: float
    monthly_normals: dict | None  # None => parametric fallback active

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def has_curated_normals(self) -> bool:
        return self.monthly_normals is not None

    def normals(self, month: int) -> tuple[float, float]:
        if self.monthly_normals is not None:
            m = self.monthly_normals[month]
            return float(m[0]), float(m[1])
        return parametric_normals(self.lat, month)


_ICAO_DB: dict | None = None
_NORMALS: dict = {}
_LOADED = False


def _load():
    global _ICAO_DB, _NORMALS, _LOADED
    if _LOADED:
        return
    _ICAO_DB = airportsdata.load("ICAO")
    if NORMALS_PATH.exists():
        with open(NORMALS_PATH) as f:
            data = yaml.safe_load(f) or {}
            _NORMALS = {k.upper(): v for k, v in data.items()}
    _LOADED = True


def get_airport(icao: str) -> Airport | None:
    _load()
    icao = icao.upper()
    base = _ICAO_DB.get(icao)
    if not base:
        return None
    override = _NORMALS.get(icao, {}) or {}
    raw = override.get("monthly_normals")
    normals = {int(m): list(v) for m, v in raw.items()} if raw else None
    return Airport(
        icao=icao,
        name=base["name"],
        iata=base.get("iata") or None,
        lat=float(base["lat"]),
        lon=float(base["lon"]),
        tz=base["tz"],
        peak_local_hour=float(override.get("peak_local_hour", 15)),
        monthly_normals=normals,
    )


def load_airports() -> dict[str, Airport]:
    """Return airports that have curated normals (used to warm caches)."""
    _load()
    out = {}
    for icao in _NORMALS:
        ap = get_airport(icao)
        if ap is not None:
            out[icao] = ap
    return out


def match_airport_by_keyword(text: str):  # pragma: no cover  (deprecated)
    return None


# ---------------------------------------------------------------------------
# Parametric fallback climo (latitude × month). Fires only when an airport is
# not in airport_normals.yaml. Tuned roughly for CONUS at lat 25-50N.

def parametric_normals(lat: float, month: int) -> tuple[float, float]:
    annual_mean = 71.0 - 0.9 * max(0.0, abs(lat) - 25.0)
    swing = 13.0 + 0.55 * max(0.0, abs(lat) - 25.0)
    seasonal = swing * math.cos(2 * math.pi * (month - 7) / 12.0)
    daily_mean = annual_mean + seasonal
    diurnal_range = 18.0
    return daily_mean + diurnal_range / 2, daily_mean - diurnal_range / 2


# ---------------------------------------------------------------------------
# Diurnal curve

def diurnal_temp_f(airport: Airport, local_hour: float, month: int) -> float:
    tmax, tmin = airport.normals(month)
    sunrise = SUNRISE_HOUR
    peak = airport.peak_local_hour
    h = local_hour
    if h < sunrise:
        h += 24
    if h <= peak:
        frac = (h - sunrise) / (peak - sunrise)
        return tmin + (tmax - tmin) * 0.5 * (1 - math.cos(math.pi * frac))
    next_sunrise = sunrise + 24
    frac = (h - peak) / (next_sunrise - peak)
    return tmax - (tmax - tmin) * 0.5 * (1 - math.cos(math.pi * frac))


def climo_max_reached_by(airport: Airport, local_time: datetime) -> float:
    month = local_time.month
    tmax, tmin = airport.normals(month)
    h = local_time.hour + local_time.minute / 60.0
    if h >= airport.peak_local_hour:
        return tmax
    if h <= SUNRISE_HOUR:
        return tmin
    return diurnal_temp_f(airport, h, month)


def now_local(airport: Airport) -> datetime:
    return datetime.now(airport.zone)


def local_today_bounds_utc(airport: Airport):
    n = now_local(airport)
    start_local = n.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local.replace(hour=23, minute=59, second=59)
    return (
        start_local.astimezone(ZoneInfo("UTC")),
        end_local.astimezone(ZoneInfo("UTC")),
    )
