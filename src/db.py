"""SQLite state layer. Single-writer model; APScheduler runs jobs sequentially
in one thread so naive sqlite3 is fine. WAL mode for read concurrency."""
from __future__ import annotations
import os
import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.getenv("POLYWX_DATA_DIR", "./data")) / "polywx.db"


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    event_id          TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    description       TEXT,
    airport_icao      TEXT,        -- resolved airport from description
    local_date        TEXT,        -- YYYY-MM-DD in airport TZ
    end_date          TEXT,        -- ISO UTC
    raw               TEXT,        -- full gamma event JSON
    last_seen_utc     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buckets (
    market_id         TEXT PRIMARY KEY,    -- gamma sub-market id
    event_id          TEXT NOT NULL,
    label             TEXT NOT NULL,       -- human label, e.g. "71-75°F"
    lower_f           REAL,                -- -inf as null
    upper_f           REAL,                -- +inf as null
    yes_token_id      TEXT,
    no_token_id       TEXT,
    FOREIGN KEY(event_id) REFERENCES markets(event_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_buckets_event ON buckets(event_id);

CREATE TABLE IF NOT EXISTS metar_obs (
    icao              TEXT NOT NULL,
    obs_time_utc      TEXT NOT NULL,
    temp_f            REAL,
    PRIMARY KEY (icao, obs_time_utc)
);
CREATE INDEX IF NOT EXISTS idx_metar_icao_time ON metar_obs(icao, obs_time_utc);

CREATE TABLE IF NOT EXISTS hrrr_runs (
    icao              TEXT NOT NULL,
    run_init_utc      TEXT NOT NULL,
    local_date        TEXT NOT NULL,
    pred_max_f        REAL,
    fetched_utc       TEXT NOT NULL,
    PRIMARY KEY (icao, run_init_utc, local_date)
);

CREATE TABLE IF NOT EXISTS bucket_history (
    market_id         TEXT NOT NULL,
    ts_utc            TEXT NOT NULL,
    fair_prob         REAL NOT NULL,
    best_ask          REAL,
    obs_max_f         REAL,
    PRIMARY KEY (market_id, ts_utc)
);
CREATE INDEX IF NOT EXISTS idx_history_market ON bucket_history(market_id, ts_utc);

CREATE TABLE IF NOT EXISTS watchlist (
    market_id         TEXT PRIMARY KEY,
    pinned_utc        TEXT NOT NULL,
    last_alerted_prob REAL
);

CREATE TABLE IF NOT EXISTS alert_state (
    event_id          TEXT NOT NULL,
    leader_market_id  TEXT,
    leader_prob       REAL,
    updated_utc       TEXT NOT NULL,
    PRIMARY KEY (event_id)
);
"""


def init_schema():
    with db() as c:
        c.executescript(SCHEMA)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- markets / buckets -----------------------------------------------------

def upsert_market(event_id, title, description, airport_icao, local_date, end_date, raw):
    with db() as c:
        c.execute(
            """INSERT INTO markets(event_id, title, description, airport_icao, local_date, end_date, raw, last_seen_utc)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET
                   title=excluded.title,
                   description=excluded.description,
                   airport_icao=excluded.airport_icao,
                   local_date=excluded.local_date,
                   end_date=excluded.end_date,
                   raw=excluded.raw,
                   last_seen_utc=excluded.last_seen_utc""",
            (event_id, title, description, airport_icao, local_date, end_date,
             json.dumps(raw), now_utc()),
        )


def upsert_bucket(market_id, event_id, label, lower_f, upper_f, yes_token, no_token):
    with db() as c:
        c.execute(
            """INSERT INTO buckets(market_id, event_id, label, lower_f, upper_f, yes_token_id, no_token_id)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(market_id) DO UPDATE SET
                   label=excluded.label,
                   lower_f=excluded.lower_f,
                   upper_f=excluded.upper_f,
                   yes_token_id=excluded.yes_token_id,
                   no_token_id=excluded.no_token_id""",
            (market_id, event_id, label, lower_f, upper_f, yes_token, no_token),
        )


def list_active_events():
    with db() as c:
        rows = c.execute(
            """SELECT * FROM markets
               WHERE end_date > ?
               ORDER BY end_date ASC""",
            (now_utc(),),
        ).fetchall()
        return [dict(r) for r in rows]


def event_buckets(event_id):
    with db() as c:
        rows = c.execute(
            "SELECT * FROM buckets WHERE event_id = ? ORDER BY lower_f IS NULL DESC, lower_f ASC",
            (event_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def find_event_by_keyword(query: str):
    """Fuzzy lookup by city keyword in title/description."""
    q = f"%{query.lower()}%"
    with db() as c:
        rows = c.execute(
            """SELECT * FROM markets
               WHERE end_date > ?
                 AND (lower(title) LIKE ? OR lower(description) LIKE ?)
               ORDER BY end_date ASC""",
            (now_utc(), q, q),
        ).fetchall()
        return [dict(r) for r in rows]


# --- metar -----------------------------------------------------------------

def upsert_metar(icao, obs_time_utc, temp_f):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO metar_obs(icao, obs_time_utc, temp_f) VALUES (?,?,?)",
            (icao, obs_time_utc, temp_f),
        )


def metars_since(icao, since_utc_iso):
    with db() as c:
        rows = c.execute(
            """SELECT obs_time_utc, temp_f FROM metar_obs
               WHERE icao = ? AND obs_time_utc >= ?
               ORDER BY obs_time_utc ASC""",
            (icao, since_utc_iso),
        ).fetchall()
        return [(r["obs_time_utc"], r["temp_f"]) for r in rows]


# --- hrrr ------------------------------------------------------------------

def upsert_hrrr_pred(icao, run_init_utc, local_date, pred_max_f):
    with db() as c:
        c.execute(
            """INSERT OR REPLACE INTO hrrr_runs(icao, run_init_utc, local_date, pred_max_f, fetched_utc)
               VALUES (?,?,?,?,?)""",
            (icao, run_init_utc, local_date, pred_max_f, now_utc()),
        )


def recent_hrrr_preds(icao, local_date, limit=6):
    with db() as c:
        rows = c.execute(
            """SELECT pred_max_f, run_init_utc FROM hrrr_runs
               WHERE icao = ? AND local_date = ? AND pred_max_f IS NOT NULL
               ORDER BY run_init_utc DESC LIMIT ?""",
            (icao, local_date, limit),
        ).fetchall()
        return [(r["pred_max_f"], r["run_init_utc"]) for r in rows]


# --- bucket history --------------------------------------------------------

def record_bucket_snapshot(market_id, fair_prob, best_ask, obs_max_f):
    with db() as c:
        c.execute(
            """INSERT OR REPLACE INTO bucket_history(market_id, ts_utc, fair_prob, best_ask, obs_max_f)
               VALUES (?,?,?,?,?)""",
            (market_id, now_utc(), fair_prob, best_ask, obs_max_f),
        )


def previous_bucket_prob(market_id):
    """Return the fair_prob from the snapshot before the current one (i.e. 2nd most recent)."""
    with db() as c:
        rows = c.execute(
            "SELECT fair_prob FROM bucket_history WHERE market_id = ? ORDER BY ts_utc DESC LIMIT 2",
            (market_id,),
        ).fetchall()
        if len(rows) < 2:
            return None
        return rows[1]["fair_prob"]


# --- watchlist -------------------------------------------------------------

def add_watch(market_id):
    with db() as c:
        c.execute(
            "INSERT OR REPLACE INTO watchlist(market_id, pinned_utc, last_alerted_prob) VALUES (?,?,?)",
            (market_id, now_utc(), None),
        )


def remove_watch(market_id):
    with db() as c:
        c.execute("DELETE FROM watchlist WHERE market_id = ?", (market_id,))


def list_watches():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM watchlist").fetchall()]


def update_watch_alerted(market_id, prob):
    with db() as c:
        c.execute(
            "UPDATE watchlist SET last_alerted_prob = ? WHERE market_id = ?",
            (prob, market_id),
        )


# --- alert leader tracking -------------------------------------------------

def get_leader(event_id):
    with db() as c:
        r = c.execute("SELECT * FROM alert_state WHERE event_id = ?", (event_id,)).fetchone()
        return dict(r) if r else None


def set_leader(event_id, market_id, prob):
    with db() as c:
        c.execute(
            """INSERT OR REPLACE INTO alert_state(event_id, leader_market_id, leader_prob, updated_utc)
               VALUES (?,?,?,?)""",
            (event_id, market_id, prob, now_utc()),
        )
