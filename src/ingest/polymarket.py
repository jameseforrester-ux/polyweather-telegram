"""Polymarket ingestor.

Discovery flow per event:
  1. Pull active events tagged `daily-temperature` from Gamma (paginated).
  2. Keep only `highest temperature` markets.
  3. Resolution airport:
       a. PRIMARY: parse 4-letter ICAO from the Wunderground URL in the
          description (`wunderground.com/.../K****` or similar).
       b. FALLBACK: parse the city name from the title and look it up in
          config/intl_airports.yaml.
  4. Bucket parsing: handles both Fahrenheit (US) and Celsius (intl) labels;
     converts to °F internally.

CLOB book lookups for each YES token power the edge calculation in
analytics/edge.py.
"""
from __future__ import annotations
import os
import re
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
import httpx
import yaml

from .. import db
from .climatology import get_airport

log = logging.getLogger(__name__)

GAMMA_URL = os.getenv("POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com")
CLOB_URL = os.getenv("POLYMARKET_CLOB_URL", "https://clob.polymarket.com")

TITLE_HINTS = ("highest temperature", "high temperature", "hottest day", "high temp")

# ---------------------------------------------------------------------------
# Airport resolution — two-stage

# Any 4-letter ICAO in a Wunderground URL. Used to be K-only; now broader so
# we catch international stations Polymarket sometimes links.
RX_ICAO_FROM_WUNDERGROUND = re.compile(
    r"wunderground\.com[^\s\"'<>]*?/([A-Z]{4})\b", re.I
)

# Title format: "Highest temperature in <City> on <Date>?"
RX_CITY_FROM_TITLE = re.compile(
    r"highest temperature in\s+(.+?)\s+on\b", re.I
)

_CITY_TO_ICAO: dict[str, str] | None = None


def _load_city_map() -> dict[str, str]:
    global _CITY_TO_ICAO
    if _CITY_TO_ICAO is not None:
        return _CITY_TO_ICAO
    path = Path(__file__).parent.parent.parent / "config" / "intl_airports.yaml"
    if not path.exists():
        _CITY_TO_ICAO = {}
        return _CITY_TO_ICAO
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    _CITY_TO_ICAO = {k.strip().lower(): v.strip().upper() for k, v in data.items()}
    return _CITY_TO_ICAO


def parse_resolution_icao(description: str) -> str | None:
    if not description:
        return None
    m = RX_ICAO_FROM_WUNDERGROUND.search(description)
    return m.group(1).upper() if m else None


def parse_city_from_title(title: str) -> str | None:
    if not title:
        return None
    m = RX_CITY_FROM_TITLE.search(title)
    return m.group(1).strip() if m else None


def resolve_airport_icao(title: str, description: str) -> tuple[str | None, str]:
    """Returns (icao, source). source is 'url', 'city', or 'unresolved'."""
    icao = parse_resolution_icao(description)
    if icao:
        return icao, "url"
    city = parse_city_from_title(title)
    if city:
        mapping = _load_city_map()
        icao = mapping.get(city.lower())
        if icao:
            return icao, "city"
    return None, "unresolved"


# ---------------------------------------------------------------------------
# Bucket parsing — supports °F (US) and °C (international)

RX_RANGE_F    = re.compile(r"(\d{2,3})\s*[-–—]\s*(\d{2,3})\s*°?\s*f", re.I)
RX_RANGE_C    = re.compile(r"(-?\d{1,3})\s*[-–—]\s*(-?\d{1,3})\s*°?\s*c", re.I)
RX_RANGE_ANY  = re.compile(r"(-?\d{1,3})\s*[-–—to]+\s*(-?\d{1,3})")

RX_OR_LOWER_F = re.compile(r"(-?\d{1,3})\s*°?\s*f?\s*(?:or\s+lower|or\s+below|or\s+less|or\s+colder)", re.I)
RX_OR_LOWER_C = re.compile(r"(-?\d{1,3})\s*°?\s*c\s*(?:or\s+lower|or\s+below|or\s+less|or\s+colder)", re.I)
RX_BELOW      = re.compile(r"(?:below|under|less than|<)\s*(-?\d{1,3})", re.I)

RX_OR_HIGHER_F= re.compile(r"(-?\d{1,3})\s*°?\s*f?\s*(?:or\s+higher|or\s+above|or\s+more|or\s+hotter|\+)", re.I)
RX_OR_HIGHER_C= re.compile(r"(-?\d{1,3})\s*°?\s*c\s*(?:or\s+higher|or\s+above|or\s+more|or\s+hotter|\+)", re.I)
RX_ABOVE      = re.compile(r"(?:above|over|more than|>)\s*(-?\d{1,3})", re.I)


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _label_is_celsius(label: str) -> bool:
    t = label.lower()
    if "°c" in t or "celsius" in t:
        return True
    if "°f" in t or "fahrenheit" in t:
        return False
    # Heuristic: if all numbers are ≤ 50, assume Celsius (no US high-temp
    # market goes that low; no European market goes that high in °C).
    nums = re.findall(r"-?\d{1,3}", t)
    if nums:
        try:
            if max(int(n) for n in nums) <= 50:
                return True
        except ValueError:
            pass
    return False


def parse_bucket(label: str) -> tuple[float | None, float | None] | None:
    """Return (lower_f, upper_f). None on either side = unbounded."""
    if not label:
        return None
    celsius = _label_is_celsius(label)
    t = label.lower()

    def _convert(x: float) -> float:
        return _c_to_f(x) if celsius else float(x)

    # Range (e.g. "70-74", "71-75°F", "20-22°C")
    m = RX_RANGE_C.search(t) if celsius else RX_RANGE_F.search(t)
    if not m:
        m = RX_RANGE_ANY.search(t)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return _convert(min(lo, hi)), _convert(max(lo, hi))

    # Upper-only (≤ X)
    m = RX_OR_LOWER_C.search(t) if celsius else RX_OR_LOWER_F.search(t)
    if not m and not celsius:
        m = RX_OR_LOWER_F.search(t)
    if m:
        return None, _convert(int(m.group(1)))
    m = RX_BELOW.search(t)
    if m:
        return None, _convert(int(m.group(1)) - 0.5)

    # Lower-only (≥ X)
    m = RX_OR_HIGHER_C.search(t) if celsius else RX_OR_HIGHER_F.search(t)
    if not m and not celsius:
        m = RX_OR_HIGHER_F.search(t)
    if m:
        return _convert(int(m.group(1))), None
    m = RX_ABOVE.search(t)
    if m:
        return _convert(int(m.group(1)) + 0.5), None

    return None


# ---------------------------------------------------------------------------
# Gamma discovery with pagination

async def _gamma_events(max_total: int = 500) -> list[dict]:
    """Paginated fetch of every active daily-temperature event."""
    out: list[dict] = []
    page_size = 100
    async with httpx.AsyncClient(timeout=20.0) as c:
        offset = 0
        while offset < max_total:
            try:
                r = await c.get(
                    f"{GAMMA_URL}/events",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": str(page_size),
                        "offset": str(offset),
                        "tag_slug": "daily-temperature",
                    },
                )
                r.raise_for_status()
                data = r.json()
                page = data if isinstance(data, list) else (data.get("data") or [])
            except Exception as e:
                log.warning("Gamma daily-temperature page offset=%d failed: %s", offset, e)
                break
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
    return out


def _looks_like_high_temp(event: dict) -> bool:
    title = (event.get("title") or "").lower()
    return any(h in title for h in TITLE_HINTS) and "lowest" not in title


def _clob_token_ids(market: dict) -> tuple[str | None, str | None]:
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
    return (
        market.get("groupItemTitle")
        or market.get("question")
        or market.get("slug")
        or ""
    )


async def ingest_events() -> int:
    events = await _gamma_events()
    n_events = 0
    for ev in events:
        if not _looks_like_high_temp(ev):
            continue

        event_id = str(ev.get("id"))
        title = ev.get("title") or ""
        description = ev.get("description") or ""

        icao, source = resolve_airport_icao(title, description)
        if icao is None:
            log.info("Could not resolve airport for event %s (%r) — skipping", event_id, title[:80])
            continue
        ap = get_airport(icao)
        if ap is None:
            log.warning("ICAO %s (source=%s) not in airportsdata for event %s — skipping",
                        icao, source, event_id)
            continue
        if not ap.has_curated_normals:
            log.info("Parametric fallback climo: %s (%s) — add normals to "
                     "config/airport_normals.yaml for higher accuracy",
                     icao, ap.name)
        if source == "city":
            log.info("Event %s resolved by city name → %s (%s). If wrong, "
                     "override in config/intl_airports.yaml.",
                     event_id, icao, ap.name)

        end_date = ev.get("endDate") or ev.get("end_date")
        if not end_date:
            continue
        try:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except Exception:
            continue
        if end_dt <= datetime.now(timezone.utc):
            continue

        local_date = end_dt.astimezone(ap.zone).date().isoformat()

        db.upsert_market(event_id, title, description, ap.icao, local_date,
                         end_dt.isoformat(), ev)

        n_buckets = 0
        for m in ev.get("markets", []) or []:
            mkt_id = str(m.get("id"))
            label = _bucket_label(m)
            bounds = parse_bucket(label)
            if bounds is None:
                log.debug("Unparseable bucket: %r (event %s, market %s)",
                          label, event_id, mkt_id)
                continue
            yes_tok, no_tok = _clob_token_ids(m)
            db.upsert_bucket(mkt_id, event_id, label, bounds[0], bounds[1], yes_tok, no_tok)
            n_buckets += 1

        log.debug("Event %s (%s): %d buckets", event_id, ap.icao, n_buckets)
        n_events += 1
    log.info("Polymarket ingest: %d high-temp events upserted", n_events)
    return n_events


# ---------------------------------------------------------------------------
# CLOB order book

async def get_best_ask(token_id: str) -> float | None:
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
        prices = []
        for a in asks:
            try:
                prices.append(float(a["price"]))
            except (KeyError, TypeError, ValueError):
                continue
        return min(prices) if prices else None
    except Exception as e:
        log.debug("CLOB book fetch failed for token %s: %s", token_id, e)
        return None
