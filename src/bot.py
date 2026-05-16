"""Telegram bot handlers.

Owner-only (single user). Ignores chat_ids other than TELEGRAM_OWNER_CHAT_ID.

Navigation model:
  / commands and reply-keyboard buttons open the four main screens:
    🌡 Markets  -> paginated alphabetical city list
    🔥 Top Opps -> top edge opportunities (fair_prob>=85%, edge>=+5¢)
    👁 Watched  -> pinned buckets
    📊 Status   -> data freshness

  Inside Markets:  city button -> list of days that city has open ->
                   day button  -> existing bucket detail (fair / ask / edge).

Callback data scheme (under 64 bytes each):
  cities:<page>          -> show cities page
  city:<icao>            -> show days for that airport
  ev:<event_id>          -> show event detail
  refresh:<event_id>     -> recompute & re-render event
  top:<page>             -> top-opportunities page
  watch:<market_id>      -> pin a bucket
  unwatch:<market_id>    -> unpin a bucket
  back                   -> back to cities page 0
  back:city:<icao>       -> back to days-for-city
  noop                   -> placeholder (page indicator)
"""
from __future__ import annotations
import os
import re
import logging
from datetime import datetime, timezone

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters,
)

from . import db
from .analytics.edge import compute_event_view, EventView, BucketView
from .alerts.triggers import get_cached_views, get_cached_view
from .ingest.climatology import get_airport, now_local

log = logging.getLogger(__name__)

OWNER_CHAT_ID = int(os.getenv("TELEGRAM_OWNER_CHAT_ID", "0"))
PAGE_SIZE = 15
TOP_DEFAULT_N = 10
TOP_MIN_FAIR = 0.85
TOP_MIN_EDGE = 0.05

RX_CITY_FROM_TITLE = re.compile(r"in\s+(.+?)\s+on\b", re.I)


# ---------------------------------------------------------------------------
# Auth gate

def _is_owner(update: Update) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    if OWNER_CHAT_ID == 0:
        log.warning("TELEGRAM_OWNER_CHAT_ID not set; ignoring %s", chat.id)
        return False
    return chat.id == OWNER_CHAT_ID


async def _deny(update: Update):
    if update.callback_query:
        await update.callback_query.answer("Not authorized.", show_alert=True)
    elif update.message:
        await update.message.reply_text("Not authorized.")


# ---------------------------------------------------------------------------
# Persistent reply keyboard

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("🌡 Markets"), KeyboardButton("🔥 Top Opps")],
        [KeyboardButton("👁 Watched"), KeyboardButton("📊 Status")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


# ---------------------------------------------------------------------------
# Helpers

def _fmt_temp(v: float | None, suffix: str = "°F") -> str:
    return "—" if v is None else f"{v:.1f}{suffix}"


def _city_from_title(title: str) -> str:
    m = RX_CITY_FROM_TITLE.search(title or "")
    return m.group(1).strip() if m else "?"


def _cities_with_events() -> list[tuple[str, str, int]]:
    """Returns [(icao, city_display, n_days), ...] sorted alphabetically by city."""
    events = db.list_active_events()
    by_icao: dict[str, list[dict]] = {}
    for ev in events:
        icao = ev["airport_icao"]
        if not icao:
            continue
        by_icao.setdefault(icao, []).append(ev)
    out = []
    for icao, evs in by_icao.items():
        city = _city_from_title(evs[0]["title"])
        out.append((icao, city, len(evs)))
    out.sort(key=lambda t: (t[1].lower(), t[0]))
    return out


async def _send_or_edit(update: Update, text: str, kb: InlineKeyboardMarkup | None):
    """Send a new message if from /command, edit if from a callback button."""
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb
            )
            return
        except Exception:
            # Edit failed (e.g. same content) — fall through to send
            pass
    target = update.effective_message
    await target.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


# ---------------------------------------------------------------------------
# Screen: cities list (paginated)

def _cities_keyboard(cities: list[tuple[str, str, int]], page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(cities) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = cities[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = []
    for icao, city, n_days in chunk:
        days_str = f"{n_days}d" if n_days > 1 else "1d"
        rows.append([InlineKeyboardButton(
            f"{city}  ·  {icao}  ·  {days_str}",
            callback_data=f"city:{icao}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"cities:{page-1}"))
    nav.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next »", callback_data=f"cities:{page+1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def show_cities(update: Update, page: int = 0):
    cities = _cities_with_events()
    if not cities:
        await _send_or_edit(update, "*Markets*\n\n_No active markets yet._", None)
        return
    total_events = sum(n for _, _, n in cities)
    text = f"*Markets by city*\n{len(cities)} cities · {total_events} events"
    await _send_or_edit(update, text, _cities_keyboard(cities, page))


# ---------------------------------------------------------------------------
# Screen: days for a city

async def show_city_days(update: Update, icao: str):
    events = [e for e in db.list_active_events() if e["airport_icao"] == icao]
    events.sort(key=lambda e: e["local_date"])
    ap = get_airport(icao)
    if not events:
        await _send_or_edit(update, f"*{icao}*\n\n_No active markets for this airport._",
                            InlineKeyboardMarkup([[InlineKeyboardButton("« Cities", callback_data="cities:0")]]))
        return
    city = _city_from_title(events[0]["title"])
    rows = []
    for ev in events:
        rows.append([InlineKeyboardButton(
            ev["local_date"],
            callback_data=f"ev:{ev['event_id']}",
        )])
    rows.append([InlineKeyboardButton("« Cities", callback_data="cities:0")])
    title = f"*{city}*  ·  `{icao}`"
    if ap:
        title += f"\n_{ap.name}_"
    title += f"\n\nSelect a day:"
    await _send_or_edit(update, title, InlineKeyboardMarkup(rows))


# ---------------------------------------------------------------------------
# Screen: event detail (bucket table)

def _fmt_event_view(view: EventView) -> str:
    city = _city_from_title(view.title)
    lines = [
        f"*{city}*  ·  `{view.airport_icao}`  ·  `{view.local_date}`",
        "",
        f"Obs max so far:  *{_fmt_temp(view.obs_max_f)}*",
        f"NWP latest run: {_fmt_temp(view.hrrr_pred_max_f)}",
        f"Fair max (blended): *{_fmt_temp(view.fair_max_f)}*  ±{view.sigma_f:.1f}°F",
        "",
        "`bucket          fair    ask    edge`",
    ]
    for b in view.buckets:
        ask_str = "  —  " if b.best_ask is None else f"{b.best_ask*100:5.1f}¢"
        lines.append(
            f"`{b.label:<14s} {b.fair_prob*100:5.1f}%  {ask_str}  {b.edge_str():>7s}`"
        )
    lines.append("")
    ap = get_airport(view.airport_icao)
    if ap:
        lines.append(f"_Local: {now_local(ap).strftime('%Y-%m-%d %H:%M %Z')}_")
    return "\n".join(lines)


def _event_keyboard(view: EventView) -> InlineKeyboardMarkup:
    watched = {w["market_id"] for w in db.list_watches()}
    rows = []
    for b in view.buckets:
        is_watched = b.market_id in watched
        rows.append([
            InlineKeyboardButton(("👁  " if is_watched else "    ") + b.label,
                                 callback_data="noop"),
            InlineKeyboardButton(
                "Unwatch" if is_watched else "Watch",
                callback_data=("unwatch:" if is_watched else "watch:") + b.market_id,
            ),
        ])
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{view.event_id}"),
        InlineKeyboardButton("« Days", callback_data=f"back:city:{view.airport_icao}"),
    ])
    return InlineKeyboardMarkup(rows)


async def show_event(update: Update, event_id: str):
    # Try cache first (cheap), fall back to live recompute (slower but works for new events)
    view = get_cached_view(event_id)
    if view is None:
        try:
            view = await compute_event_view(event_id)
        except Exception as e:
            log.exception("Event view error: %s", e)
            view = None
    if view is None:
        await _send_or_edit(update, "_Failed to compute event view._", None)
        return
    await _send_or_edit(update, _fmt_event_view(view), _event_keyboard(view))


# ---------------------------------------------------------------------------
# Screen: top opportunities

def _gather_top_opportunities() -> list[tuple[EventView, BucketView]]:
    out = []
    for view in get_cached_views():
        for b in view.buckets:
            if b.fair_prob < TOP_MIN_FAIR:
                continue
            if b.edge is None or b.edge < TOP_MIN_EDGE:
                continue
            out.append((view, b))
    out.sort(key=lambda t: t[1].edge or 0.0, reverse=True)
    return out


def _top_keyboard(opps: list[tuple[EventView, BucketView]], page: int) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(opps) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = opps[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = []
    for view, b in chunk:
        city = _city_from_title(view.title)
        edge_str = b.edge_str()
        label = f"{city} {view.local_date[5:]} · {b.label} · {edge_str}"
        rows.append([InlineKeyboardButton(label[:64],
                                          callback_data=f"ev:{view.event_id}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("« Prev", callback_data=f"top:{page-1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next »", callback_data=f"top:{page+1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def show_top(update: Update, page: int = 0):
    if not get_cached_views():
        await _send_or_edit(
            update,
            "*🔥 Top Opportunities*\n\n_Cache is empty — bot is still computing "
            "the first cycle. Try again in ~1 minute._",
            None,
        )
        return
    opps = _gather_top_opportunities()
    if not opps:
        await _send_or_edit(
            update,
            f"*🔥 Top Opportunities*\n\n_No buckets currently match the filter:_\n"
            f"`fair_prob ≥ {TOP_MIN_FAIR*100:.0f}%  AND  edge ≥ +{TOP_MIN_EDGE*100:.0f}¢`\n\n"
            f"_This is normal early in the day — confidence builds as observations come in._",
            None,
        )
        return
    text = (
        f"*🔥 Top Opportunities*  ({len(opps)})\n"
        f"_fair ≥ {TOP_MIN_FAIR*100:.0f}%, edge ≥ +{TOP_MIN_EDGE*100:.0f}¢, ranked by edge_"
    )
    await _send_or_edit(update, text, _top_keyboard(opps, page))


# ---------------------------------------------------------------------------
# Screen: watches

async def show_watches(update: Update):
    ws = db.list_watches()
    if not ws:
        await _send_or_edit(update, "*👁 Watched buckets*\n\n_None pinned._", None)
        return
    lines = ["*👁 Watched buckets*", ""]
    rows = []
    for w in ws:
        with db.db() as c:
            r = c.execute(
                """SELECT b.label, m.title, m.event_id, m.airport_icao, m.local_date
                   FROM buckets b JOIN markets m ON b.event_id = m.event_id
                   WHERE b.market_id = ?""", (w["market_id"],)
            ).fetchone()
        if r is None:
            continue
        city = _city_from_title(r["title"])
        lines.append(f"• {city} ({r['airport_icao']}) {r['local_date']} — {r['label']}")
        rows.append([
            InlineKeyboardButton(f"Open {city} {r['local_date']}",
                                 callback_data=f"ev:{r['event_id']}"),
            InlineKeyboardButton("Unpin", callback_data=f"unwatch:{w['market_id']}"),
        ])
    kb = InlineKeyboardMarkup(rows) if rows else None
    await _send_or_edit(update, "\n".join(lines), kb)


# ---------------------------------------------------------------------------
# Screen: status

async def show_status(update: Update):
    events = db.list_active_events()
    icaos = sorted({e["airport_icao"] for e in events if e["airport_icao"]})
    lines = [f"*📊 Status*  ·  {len(events)} active events  ·  {len(icaos)} airports", ""]
    cached = get_cached_views()
    lines.append(f"Fair-value cache: *{len(cached)}* views populated")
    lines.append("")
    with db.db() as c:
        for icao in icaos[:25]:  # cap to avoid telegram 4096 char limit
            r = c.execute(
                "SELECT MAX(obs_time_utc) AS t FROM metar_obs WHERE icao = ?", (icao,)
            ).fetchone()
            h = c.execute(
                "SELECT MAX(run_init_utc) AS init FROM hrrr_runs WHERE icao = ?", (icao,)
            ).fetchone()
            lines.append(f"`{icao}`  METAR: {r['t'] or '—'}  ·  NWP: {h['init'] or '—'}")
    if len(icaos) > 25:
        lines.append(f"\n_…and {len(icaos)-25} more airports._")
    await _send_or_edit(update, "\n".join(lines), None)


# ---------------------------------------------------------------------------
# Command handlers (also called by reply-keyboard text handler)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    text = (
        "*PolyWX bot online.*\n\n"
        "Use the keyboard below or these commands:\n"
        "  /markets — cities with active markets\n"
        "  /top — top edge opportunities\n"
        "  /watches — pinned buckets\n"
        "  /status — data freshness\n"
        "  /fair <city> — quick lookup by city name"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=MAIN_KEYBOARD)


async def cmd_markets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    await show_cities(update, page=0)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    await show_top(update, page=0)


async def cmd_watches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    await show_watches(update)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    await show_status(update)


async def cmd_fair(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick lookup: /fair nyc -> jumps straight into the first matching event."""
    if not _is_owner(update):
        await _deny(update)
        return
    if not context.args:
        await update.message.reply_text("Usage: `/fair <city>`", parse_mode=ParseMode.MARKDOWN)
        return
    q = " ".join(context.args).lower()
    hits = [e for e in db.list_active_events()
            if q in (e["title"] or "").lower() or q == (e["airport_icao"] or "").lower()]
    if not hits:
        await update.message.reply_text(f"No active market matches `{q}`.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    hits.sort(key=lambda e: e["local_date"])
    await show_event(update, hits[0]["event_id"])


# ---------------------------------------------------------------------------
# Reply-keyboard text router

QUICK_BUTTONS = {
    "🌡 Markets": cmd_markets,
    "🔥 Top Opps": cmd_top,
    "👁 Watched": cmd_watches,
    "📊 Status": cmd_status,
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    if update.message is None or update.message.text is None:
        return
    handler = QUICK_BUTTONS.get(update.message.text.strip())
    if handler is None:
        return
    await handler(update, context)


# ---------------------------------------------------------------------------
# Callback handler (in-screen navigation)

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        await _deny(update)
        return
    q = update.callback_query
    await q.answer()
    data = q.data or ""

    if data == "noop":
        return

    if data == "back":
        await show_cities(update, page=0)
        return

    if data.startswith("back:city:"):
        icao = data[len("back:city:"):]
        await show_city_days(update, icao)
        return

    if data.startswith("cities:"):
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        await show_cities(update, page=page)
        return

    if data.startswith("city:"):
        icao = data.split(":", 1)[1]
        await show_city_days(update, icao)
        return

    if data.startswith("top:"):
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            page = 0
        await show_top(update, page=page)
        return

    if data.startswith("ev:") or data.startswith("refresh:"):
        event_id = data.split(":", 1)[1]
        await show_event(update, event_id)
        return

    if data.startswith("watch:"):
        market_id = data.split(":", 1)[1]
        db.add_watch(market_id)
        with db.db() as c:
            r = c.execute("SELECT event_id FROM buckets WHERE market_id = ?",
                          (market_id,)).fetchone()
        if r:
            await show_event(update, r["event_id"])
        return

    if data.startswith("unwatch:"):
        market_id = data.split(":", 1)[1]
        db.remove_watch(market_id)
        with db.db() as c:
            r = c.execute("SELECT event_id FROM buckets WHERE market_id = ?",
                          (market_id,)).fetchone()
        if r:
            await show_event(update, r["event_id"])
        return


# ---------------------------------------------------------------------------
# Wiring

def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("markets", cmd_markets))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("watches", cmd_watches))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("fair", cmd_fair))
    app.add_handler(CallbackQueryHandler(on_callback))
    # Catches reply-keyboard taps (which arrive as plain-text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
