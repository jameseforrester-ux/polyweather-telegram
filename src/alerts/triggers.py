"""Alert detection.

Runs after every fair-value recompute. For each active event:

  1. Identify the current leader bucket (highest fair_prob).
  2. If the previous leader's prob dropped ≥ ALERT_BUCKET_PROB_DROP (absolute),
     emit a "leader weakening" alert.
  3. For each pinned bucket on the watchlist:
       - If its fair_prob dropped ≥ threshold since last alert: emit.
       - If obs_max_so_far > bucket.upper_f: emit BUSTED.
       - If even an aggressive remaining-rise ceiling can't reach
         bucket.lower_f: emit FLOOR_UNREACHABLE.
"""
from __future__ import annotations
import os
import logging
from dataclasses import dataclass

from .. import db
from ..ingest.climatology import get_airport, now_local, climo_max_reached_by
from ..analytics.edge import compute_event_view, EventView, BucketView

log = logging.getLogger(__name__)

DROP_THRESHOLD = float(os.getenv("ALERT_BUCKET_PROB_DROP", "0.15"))
MIN_FAIR_FOR_WATCH = float(os.getenv("ALERT_MIN_FAIR_FOR_WATCH", "0.05"))


@dataclass
class Alert:
    kind: str       # "LEADER_DROP" | "WATCH_DROP" | "BUSTED" | "FLOOR_UNREACHABLE"
    title: str      # event title
    bucket_label: str
    detail: str


def _max_remaining_rise_ceiling(airport, obs_max_f: float) -> float:
    """Aggressive upper bound on how much more the temp can rise today.
    Uses 1.5× the climatological remaining rise as the ceiling."""
    n = now_local(airport)
    climo_now = climo_max_reached_by(airport, n)
    climo_daily_max, _ = airport.normals(n.month)
    rem = max(0.0, climo_daily_max - climo_now)
    return obs_max_f + rem * 1.5


def _evaluate_event(view: EventView) -> list[Alert]:
    out: list[Alert] = []

    # ---- Leader bucket dynamics ----
    leader = max(view.buckets, key=lambda b: b.fair_prob) if view.buckets else None
    prev_state = db.get_leader(view.event_id)

    if leader is not None and leader.fair_prob > 0:
        if prev_state and prev_state["leader_market_id"] == leader.market_id:
            prev_prob = prev_state["leader_prob"] or 0.0
            drop = prev_prob - leader.fair_prob
            if drop >= DROP_THRESHOLD:
                out.append(Alert(
                    kind="LEADER_DROP",
                    title=view.title,
                    bucket_label=leader.label,
                    detail=(f"Leader {leader.label}: {prev_prob*100:.0f}% → "
                            f"{leader.fair_prob*100:.0f}%  (Δ -{drop*100:.0f}pp). "
                            f"Likely not the final window."),
                ))
        elif prev_state and prev_state["leader_market_id"] != leader.market_id:
            # Leadership changed entirely
            out.append(Alert(
                kind="LEADER_DROP",
                title=view.title,
                bucket_label=leader.label,
                detail=f"New leader: {leader.label} at {leader.fair_prob*100:.0f}%.",
            ))
        db.set_leader(view.event_id, leader.market_id, leader.fair_prob)

    # ---- Watched buckets ----
    watches = {w["market_id"]: w for w in db.list_watches()}
    airport = get_airport(view.airport_icao)
    for bv in view.buckets:
        if bv.market_id not in watches:
            continue
        w = watches[bv.market_id]

        # Busted by observation
        if (view.obs_max_f is not None and bv.upper_f is not None
                and view.obs_max_f > bv.upper_f):
            out.append(Alert(
                kind="BUSTED",
                title=view.title,
                bucket_label=bv.label,
                detail=(f"BUSTED — observed max so far {view.obs_max_f:.1f}°F > "
                        f"bucket upper {bv.upper_f:.1f}°F."),
            ))
            db.update_watch_alerted(bv.market_id, bv.fair_prob)
            continue

        # Floor unreachable
        if (view.obs_max_f is not None and bv.lower_f is not None and airport is not None):
            ceiling = _max_remaining_rise_ceiling(airport, view.obs_max_f)
            if ceiling < bv.lower_f:
                out.append(Alert(
                    kind="FLOOR_UNREACHABLE",
                    title=view.title,
                    bucket_label=bv.label,
                    detail=(f"FLOOR UNREACHABLE — max plausible rise from "
                            f"{view.obs_max_f:.1f}°F is ~{ceiling:.1f}°F, "
                            f"below bucket floor {bv.lower_f:.1f}°F."),
                ))
                db.update_watch_alerted(bv.market_id, bv.fair_prob)
                continue

        # Prob drop since last alert
        last = w.get("last_alerted_prob")
        if last is not None and bv.fair_prob >= MIN_FAIR_FOR_WATCH:
            if (last - bv.fair_prob) >= DROP_THRESHOLD:
                out.append(Alert(
                    kind="WATCH_DROP",
                    title=view.title,
                    bucket_label=bv.label,
                    detail=(f"Watched {bv.label} dropped {last*100:.0f}% → "
                            f"{bv.fair_prob*100:.0f}%."),
                ))
                db.update_watch_alerted(bv.market_id, bv.fair_prob)
        elif last is None:
            db.update_watch_alerted(bv.market_id, bv.fair_prob)

    return out


async def evaluate_all() -> list[Alert]:
    out: list[Alert] = []
    for ev in db.list_active_events():
        try:
            view = await compute_event_view(ev["event_id"], fetch_asks=False)
        except Exception as e:
            log.exception("Event view failed for %s: %s", ev["event_id"], e)
            continue
        if view is None:
            continue

        # snapshot histories per bucket (light, just leader's prob for now)
        if view.buckets:
            leader = max(view.buckets, key=lambda b: b.fair_prob)
            db.record_bucket_snapshot(
                leader.market_id, leader.fair_prob, leader.best_ask, view.obs_max_f
            )

        out.extend(_evaluate_event(view))
    return out
