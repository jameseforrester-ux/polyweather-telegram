"""Edge computation.

For each bucket of an event, compute:
    fair_prob  : from our truncated-normal distribution
    best_ask   : lowest CLOB ask price on the YES token
    edge       : fair_prob - best_ask  (positive = ask is cheap)
"""
from __future__ import annotations
import asyncio
import logging
from dataclasses import dataclass

from .. import db
from ..ingest import polymarket as pm
from ..ingest.climatology import get_airport, now_local
from ..ingest.metar import today_max_obs_f
from .nowcast import fair_max
from .hrrr_error import combined_sigma
from .distribution import bucket_probs

log = logging.getLogger(__name__)


@dataclass
class BucketView:
    market_id: str
    label: str
    lower_f: float | None
    upper_f: float | None
    fair_prob: float
    best_ask: float | None
    edge: float | None  # fair - best_ask

    def edge_str(self) -> str:
        if self.edge is None:
            return "—"
        sign = "+" if self.edge >= 0 else ""
        return f"{sign}{self.edge*100:.1f}¢"


@dataclass
class EventView:
    event_id: str
    title: str
    airport_icao: str
    local_date: str
    fair_max_f: float | None
    sigma_f: float
    obs_max_f: float | None
    hrrr_pred_max_f: float | None
    buckets: list[BucketView]


async def compute_event_view(event_id: str, fetch_asks: bool = True) -> EventView | None:
    """Build the full picture for one event."""
    events = [e for e in db.list_active_events() if e["event_id"] == event_id]
    if not events:
        return None
    ev = events[0]

    airport = get_airport(ev["airport_icao"])
    if airport is None:
        log.warning("Unknown airport %s for event %s", ev["airport_icao"], event_id)
        return None

    obs_max, _ = today_max_obs_f(airport)
    preds = db.recent_hrrr_preds(airport.icao, ev["local_date"], limit=1)
    hrrr_pred = preds[0][0] if preds else None

    fm = fair_max(airport, hrrr_pred, obs_max, event_local_date=ev["local_date"])
    sigma = combined_sigma(airport, ev["local_date"], obs_max)

    raw_buckets = db.event_buckets(event_id)
    bounds = [(b["lower_f"], b["upper_f"]) for b in raw_buckets]
    if fm is not None:
        probs = bucket_probs(fm, sigma, obs_max, bounds)
    else:
        probs = [None] * len(raw_buckets)

    # Fetch asks in parallel; only for buckets with non-trivial fair prob
    # (saves CLOB calls on dead-end buckets that won't have edge anyway).
    async def _ask_or_none(tok, p):
        if not tok or p is None or p < 0.005:
            return None
        try:
            return await pm.get_best_ask(tok)
        except Exception:
            return None

    if fetch_asks:
        asks = await asyncio.gather(*[
            _ask_or_none(b["yes_token_id"], p)
            for b, p in zip(raw_buckets, probs)
        ])
    else:
        asks = [None] * len(raw_buckets)

    views: list[BucketView] = []
    for b, p, ask in zip(raw_buckets, probs, asks):
        edge = None
        if p is not None and ask is not None:
            edge = p - ask
        views.append(BucketView(
            market_id=b["market_id"],
            label=b["label"],
            lower_f=b["lower_f"],
            upper_f=b["upper_f"],
            fair_prob=p if p is not None else 0.0,
            best_ask=ask,
            edge=edge,
        ))

    return EventView(
        event_id=event_id,
        title=ev["title"],
        airport_icao=airport.icao,
        local_date=ev["local_date"],
        fair_max_f=fm,
        sigma_f=sigma,
        obs_max_f=obs_max,
        hrrr_pred_max_f=hrrr_pred,
        buckets=views,
    )
