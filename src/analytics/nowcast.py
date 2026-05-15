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
    local_time: datetime | None = None,
) -> float | None:
    """Blended point estimate for today's daily max, °F.

    Returns None only if we have neither forecast nor observation.
    """
    n = local_time or now_local(airport)
    peak = airport.peak_local_hour
    h = n.hour + n.minute / 60.0

    # Peak past: observation is the answer.
    if obs_max_f is not None and h >= peak:
        return obs_max_f

    # Observation-driven projection
    obs_proj = None
    if obs_max_f is not None:
        obs_proj = anomaly_projection(airport, obs_max_f, n)

    # Blend
    w = time_of_day_obs_weight(airport, n)
    if hrrr_pred_max_f is not None and obs_proj is not None:
        blended = w * obs_proj + (1.0 - w) * hrrr_pred_max_f
    elif obs_proj is not None:
        blended = obs_proj
    elif hrrr_pred_max_f is not None:
        blended = hrrr_pred_max_f
    else:
        return None

    # Floor at observation
    if obs_max_f is not None:
        blended = max(blended, obs_max_f)
    return blended
