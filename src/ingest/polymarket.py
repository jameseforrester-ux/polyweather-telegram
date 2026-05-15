"""Polymarket ingestor.

- Gamma API: discover active events tagged/keyworded as daily-high-temp markets.
  Parse the resolution airport from each event's description, and parse the
  bucket range and clob token IDs for each child market.

- CLOB API: pull live order books for the YES tokens so we can compute edge.
"""
from __future__ import annotations
import os
import re
import json
import logging
from datetime import datetime, timezone
import httpx

from .. import db
from .climatology import get_airport

log = logging.getLogger(__name__)

GAMMA_URL = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
CLOB_URL = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")

# Keywords that suggest a daily-high-temp market.
TITLE_HINTS = ("highest temperature", "high temperature", "hottest day", "high temp")

# Polymarket high-temp market descriptions always link to Wunderground at
# the resolution airport — e.g. wunderground.com/history/daily/us/co/aurora/KBKF.
# This is the authoritative way to identify the airport; do NOT guess from
# the city name in the title (Polymarket uses KBKF for "Denver", KHOU for
# "Houston", etc — non-obvious choices).
RX_ICAO_FROM_WUNDERGROUND = re.compile(
    r"wunderground\.com[^\s\"']*?/(K[A-Z]{3})\b", re.I
)


def parse_resolution_icao(description: str) -> str | None:
    if not description:
        return None
    m = RX_ICAO_FROM_WUNDERGROUND.search(description)
    return m.group(1).upper() if m else None


# ---------------------------------------------------------------------------
# Bucket parsing

RX_RANGE      = re.compile(r"(\d{2,3})\s*[-–—to]+\s*(\d{2,3})")
RX_OR_LOWER   = re.compile(r"(\d{2,3})\s*°?\s*(?:f)?\s*(?:or\s+lower|or\s+below|or\s+less|or\s+colder)", re.I)
RX_BELOW      = re.compile(r"(?:below|under|less than|<)\s*(\d{2,3})", re.I)
RX_OR_HIGHER  = re.compile(r"(\d{2,3})\s*°?\s*(?:f)?\s*(?:or\s+higher|or\s+above|or\s+more|or\s+hotter|\+)", re.I)
RX_ABOVE      = re.compile(r"(?:above|over|more than|>)\s*(\d{2,3})", re.I)


def parse_bucket(label: str) -> tuple[float | None, float | None] | None:
    """Return (lower_f, upper_f) where None means unbounded.

    Inclusive convention: lower_f <= value <= upper_f for the bucket to win.
    The integration code treats `None` as -inf or +inf.
    """
    t = label.lower()

    m = RX_RANGE.search(t)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return float(min(lo, hi)), float(max(lo, hi))

    m = RX_OR_LOWER.search(t)
    if m:
        return None, float(m.group(1))
    m = RX_BELOW.search(t)
    if m:
        # "below 70" = ≤ 69 in practice; treat as upper bound (69.5 for continuous)
        return None, float(m.group(1)) - 0.5

    m = RX_OR_HIGHER.search(t)
    if m:
        return float(m.group(1)), None
    m = RX_ABOVE.search(t)
    if m:
        return float(m.group(1)) + 0.5, None

    return None


# ---------------------------------------------------------------------------
# Gamma discovery

async def _gamma_events(limit: int = 200) -> list[dict]:
    """Pull active, open weather/temperature events. We over-fetch and filter by title."""
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=20.0) as c:
        # Gamma supports keyword search via `search`, plus active/closed filters.
        for kw in ("high temperature", "highest temperature"):
            try:
                r = await c.get(
                    f"{GAMMA_URL}/events",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": str(limit),
                        "search": kw,
                    },
                )
                r.raise_for_status()
                data = r.json()
                if isinstance(data, list):
                    out.extend(data)
                elif isinstance(data, dict) and "data" in data:
                    out.extend(data["data"])
            except Exception as e:
                log.warning("Gamma search failed for %r: %s", kw, e)
    # dedupe by id
    seen = set()
    deduped = []
    for e in out:
        eid = str(e.get("id"))
        if eid in seen:
            continue
        seen.add(eid)
        deduped.append(e)
    return deduped


def _looks_like_high_temp(event: dict) -> bool:
    blob = ((event.get("title") or "") + " " + (event.get("description") or "")).lower()
    return any(h in blob for h in TITLE_HINTS)


def _clob_token_ids(market: dict) -> tuple[str | None, str | None]:
    """`clobTokenIds` is sometimes a JSON-encoded string, sometimes a list."""
    raw = market.get("clobTokenIds")
    if not raw:
        return None, None
    try:
        ids = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(ids, list) and len(ids) >= 2:
            return str(ids[0]), str(ids[1])
        if isinstance(ids, list) and len(ids) == 1:
            return str(ids[0]), None
    except Exception:
        pass
    return None, None


def _bucket_label(market: dict) -> str:
    # Polymarket uses `groupItemTitle` for the sub-bucket label within an event.
    return (
        market.get("groupItemTitle")
        or market.get("question")
        or market.get("slug")
        or ""
    )


async def ingest_events() -> int:
    """Discover and persist active daily-high-temp events + their buckets."""
    events = await _gamma_events()
    n_events = 0
    for ev in events:
        if not _looks_like_high_temp(ev):
            continue

        event_id = str(ev.get("id"))
        title = ev.get("title") or ""
        description = ev.get("description") or ""

        # Resolution airport: parse from Wunderground URL (source of truth).
        icao = parse_resolution_icao(description)
        if icao is None:
            log.info("No Wunderground URL in event %s (%r) — skipping", event_id, title[:80])
            continue
        ap = get_airport(icao)
        if ap is None:
            log.warning("ICAO %s not found in airportsdata for event %s — skipping",
                        icao, event_id)
            continue
        if not ap.has_curated_normals:
            log.info("Using parametric fallback climo for %s (%s) — add normals to "
                     "config/airport_normals.yaml for better accuracy",
                     icao, ap.name)

        end_date = ev.get("endDate") or ev.get("end_date")
        if not end_date:
            continue
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except Exception:
            continue
        if end_dt <= datetime.now(timezone.utc):
            continue

        # local date is the date in airport TZ on which the market resolves
        local_date = end_dt.astimezone(ap.zone).date().isoformat()

        db.upsert_market(event_id, title, description, ap.icao, local_date,
                         end_dt.isoformat(), ev)

        for m in ev.get("markets", []) or []:
            mkt_id = str(m.get("id"))
            label = _bucket_label(m)
            bounds = parse_bucket(label)
            if bounds is None:
                log.debug("Unparseable bucket: %r (market %s)", label, mkt_id)
                continue
            yes_tok, no_tok = _clob_token_ids(m)
            db.upsert_bucket(mkt_id, event_id, label, bounds[0], bounds[1], yes_tok, no_tok)
        n_events += 1
    log.info("Polymarket ingest: %d high-temp events upserted", n_events)
    return n_events


# ---------------------------------------------------------------------------
# CLOB order book

async def get_best_ask(token_id: str) -> float | None:
    """Best (lowest) ask price for a YES token."""
    if not token_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{CLOB_URL}/book", params={"token_id": token_id})
            r.raise_for_status()
            book = r.json()
        asks = book.get("asks") or []
        if not asks:
            return None
        # CLOB returns asks ascending; sometimes not — be defensive.
        prices = []
        for a in asks:
            try:
                prices.append(float(a["price"]))
            except (KeyError, TypeError, ValueError):
                continue
        if not prices:
            return None
        return min(prices)
    except Exception as e:
        log.debug("CLOB book fetch failed for token %s: %s", token_id, e)
        return None
