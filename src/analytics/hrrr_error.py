"""Forecast uncertainty (°F sigma).

HRRR is deterministic, so we have to construct an error envelope ourselves.
Two components:

  (a) Historical RMSE by lead hour. Seed values from public HRRR verification
      stats (Benjamin et al.); replace with your own backtest when ready.

  (b) Rolling spread across the most recent N HRRR runs' predictions of
      today's max. Captures real-time model convergence — when consecutive
      runs disagree by a lot, today is genuinely uncertain.

Combined: sigma = sqrt(hist² + rolling²).

We also widen sigma slightly early in the day (before any observations have
constrained the max) and shrink it once observations are anchoring.
"""
from __future__ import annotations
import math
import statistics
from datetime import datetime

from ..ingest.climatology import Airport, now_local, SUNRISE_HOUR
from .. import db


# Seed historical RMSE table (°F) by effective lead hour.
# Effective lead = hours from now to local peak_heating.
_HIST_RMSE_BY_LEAD = [
    (0,  1.0),
    (3,  1.4),
    (6,  1.9),
    (12, 2.6),
    (18, 3.2),
    (24, 3.6),
    (36, 4.2),
    (48, 4.8),
]


def historical_sigma(lead_hours: float) -> float:
    """Interpolate historical RMSE for the given lead."""
    if lead_hours <= 0:
        return _HIST_RMSE_BY_LEAD[0][1]
    for (l1, s1), (l2, s2) in zip(_HIST_RMSE_BY_LEAD[:-1], _HIST_RMSE_BY_LEAD[1:]):
        if l1 <= lead_hours <= l2:
            frac = (lead_hours - l1) / (l2 - l1)
            return s1 + (s2 - s1) * frac
    return _HIST_RMSE_BY_LEAD[-1][1]


def rolling_spread(airport: Airport, local_date: str, n: int = 6) -> float:
    """Stdev of recent HRRR run predictions for the same target day."""
    preds = db.recent_hrrr_preds(airport.icao, local_date, limit=n)
    values = [p for p, _ in preds if p is not None]
    if len(values) < 2:
        return 0.0
    return float(statistics.pstdev(values))


def lead_hours_to_peak(airport: Airport, event_local_date: str | None = None,
                       local_time: datetime | None = None) -> float:
    """Hours from `local_time` until peak heating on `event_local_date`.
    If date is None, defaults to today's local date."""
    n = local_time or now_local(airport)
    h = n.hour + n.minute / 60.0
    same_day_lead = max(0.0, airport.peak_local_hour - h)
    if event_local_date is None:
        return same_day_lead
    today = n.date().isoformat()
    if event_local_date == today:
        return same_day_lead
    # Forward day: distance to that day's peak in hours
    from datetime import date
    try:
        target = date.fromisoformat(event_local_date)
        delta_days = (target - n.date()).days
    except Exception:
        return same_day_lead
    if delta_days <= 0:
        return same_day_lead
    # hours from now to midnight of target, plus hours from midnight to peak
    return (24 - h) + 24 * (delta_days - 1) + airport.peak_local_hour


def combined_sigma(
    airport: Airport,
    local_date: str,
    obs_max_f: float | None,
    local_time: datetime | None = None,
) -> float:
    """Combined sigma in °F."""
    n = local_time or now_local(airport)
    lead = lead_hours_to_peak(airport, local_date, n)
    hist = historical_sigma(lead)
    rolling = rolling_spread(airport, local_date)

    sigma = math.sqrt(hist * hist + rolling * rolling)

    # Early morning bonus uncertainty before any sun-up obs exist
    h = n.hour + n.minute / 60.0
    if h < SUNRISE_HOUR and obs_max_f is None:
        sigma *= 1.15

    # Minimum sigma floor: even at the peak there's instrument and
    # rounding error (METAR is reported to whole °C → ~1°F)
    return max(sigma, 0.8)
