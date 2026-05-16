"""Anchored nowcast.

Given:
  - HRRR forecast for today's daily max (from the latest run)
  - METAR observed max so far today
  - Climatology curve

Produce a single point estimate `fair_max_f`:

  1. If peak heating is past, the observation IS the max — return obs.
  2. Otherwise, compute the anomaly-persistence projection:
         obs_anomaly       = obs_max_so_far - climo_max_reached_by_now
         projected_max     = climo_daily_max + obs_anomaly
     This captures "today is running N° hotter than typical, so project N°
     above the climo peak."
  3. Blend the projection with HRRR using a time-of-day weight that ramps the
     observation-driven projection in as we approach peak heating.
  4. Floor at obs_max_so_far (can never go below what's been observed).
"""
from __future__ import annotations
import math
from datetime import datetime

from ..ingest.climatology import Airport, now_local, climo_max_reached_by, SUNRISE_HOUR


def time_of_day_obs_weight(airport: Airport, local_time: datetime | None = None) -> float:
    """Weight on the observation-driven projection vs. raw HRRR.

    Linear ramp from 0 at local midnight to 1 at peak_local_hour, then holds at 1
    through the rest of the day. (At/after peak, observations dominate completely.)
    """
    n = local_time or now_local(airport)
    h = n.hour + n.minute / 60.0
    peak = airport.peak_local_hour
    if h <= 0 or h <= SUNRISE_HOUR:
        return 0.0
    if h >= peak:
        return 1.0
    return (h - SUNRISE_HOUR) / (peak - SUNRISE_HOUR)


def anomaly_projection(airport: Airport, obs_max_f: float,
                       local_time: datetime | None = None) -> float:
    """obs_max + (climo_daily_max - climo_max_reached_by_now)
    rearranged to: climo_daily_max + (obs_max - climo_max_reached_by_now)."""
    n = local_time or now_local(airport)
    climo_so_far = climo_max_reached_by(airport, n)
    climo_daily_max, _ = airport.normals(n.month)
    return climo_daily_max + (obs_max_f - climo_so_far)


def fair_max(
    airport: Airport,
    hrrr_pred_max_f: float | None,
    obs_max_f: float | None,
    event_local_date: str | None = None,
    local_time: datetime | None = None,
) -> float | None:
    """Blended point estimate for the daily max on `event_local_date`, °F.

    Behavior:
    - If `event_local_date` is provided and isn't today (airport-local), then
      today's observations don't constrain it — `obs_max_f` is ignored.
    - When HRRR/GFS is missing, falls back to a climo-anchored blend instead
      of letting an early-morning anomaly projection run away unchecked.
    - The anomaly projection is capped at ±15°F from climo_daily_max to
      prevent overnight-warmth artifacts producing absurd daytime peaks.
    """
    n = local_time or now_local(airport)
    today_local_date = n.date().isoformat()

    # Forward-day events: today's obs are about today, not the target day.
    if event_local_date is not None and event_local_date != today_local_date:
        obs_max_f = None

    peak = airport.peak_local_hour
    h = n.hour + n.minute / 60.0
    climo_daily_max, _ = airport.normals(n.month)

    # Forecast base: NWP if available, climo otherwise.
    forecast_base = hrrr_pred_max_f if hrrr_pred_max_f is not None else climo_daily_max

    # Peak passed and we have today's obs — observation IS the answer.
    if obs_max_f is not None and h >= peak:
        return obs_max_f

    # Observation-driven projection, capped to a sane range around climo.
    obs_proj = None
    if obs_max_f is not None:
        raw_proj = anomaly_projection(airport, obs_max_f, n)
        cap_high = climo_daily_max + 15.0
        cap_low = climo_daily_max - 15.0
        obs_proj = max(cap_low, min(cap_high, raw_proj))

    # Blend obs projection with forecast base by time-of-day weight.
    w = time_of_day_obs_weight(airport, n)
    if obs_proj is not None:
        blended = w * obs_proj + (1.0 - w) * forecast_base
    else:
        blended = forecast_base

    # Floor at today's observation if applicable.
    if obs_max_f is not None:
        blended = max(blended, obs_max_f)
    return blended
