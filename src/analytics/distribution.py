"""Map (fair_mean, sigma, obs_max) → probability per bucket.

We model today's daily-max-temp as a normal with mean=fair_mean, sigma=sigma,
truncated below at obs_max_f (since the daily max can't be less than what's
already been observed). Then integrate the truncated PDF over each bucket.

Bucket bounds use the convention (lower_f, upper_f) where None = unbounded.
The Polymarket convention is integer-degree bins, e.g. 71-75 inclusive.
For the integration we treat them as continuous [lower, upper] with half-degree
fenceposts where needed (see polymarket.parse_bucket).
"""
from __future__ import annotations
import math
from typing import Sequence

from scipy.stats import truncnorm

NEG_INF = -1e9
POS_INF = 1e9


def _bound(v: float | None, sign: int) -> float:
    if v is None:
        return POS_INF if sign > 0 else NEG_INF
    return v


def bucket_probs(
    fair_mean: float,
    sigma: float,
    obs_max_f: float | None,
    buckets: Sequence[tuple[float | None, float | None]],
) -> list[float]:
    """Returns one probability per bucket, summing to ~1.0.

    `buckets` is a list of (lower_f, upper_f). Use None for ±inf.
    """
    if sigma <= 0:
        sigma = 0.8
    truncate_at = obs_max_f if obs_max_f is not None else NEG_INF

    a = (truncate_at - fair_mean) / sigma
    b = (POS_INF - fair_mean) / sigma  # effectively +inf
    tn = truncnorm(a, b, loc=fair_mean, scale=sigma)

    probs = []
    for lo, hi in buckets:
        lo_f = _bound(lo, -1)
        hi_f = _bound(hi, +1)
        # Bucket busted by observation
        if obs_max_f is not None and hi_f <= obs_max_f:
            probs.append(0.0)
            continue
        # Effective lower bound is at least the observed max
        lo_eff = max(lo_f, truncate_at)
        if lo_eff >= hi_f:
            probs.append(0.0)
            continue
        p = float(tn.cdf(hi_f) - tn.cdf(lo_eff))
        probs.append(max(0.0, min(1.0, p)))

    # Renormalize (numerical safety; truncated cdf to +inf may be ~1.0000001)
    total = sum(probs)
    if total > 0:
        probs = [p / total for p in probs]
    return probs
