"""SQLite access: schema, connections, and small shared helpers.

Connections are short-lived and opened per operation rather than pooled. With
WAL enabled and a single-process uvicorn on one small machine, this is both
simpler and safer than sharing a connection across async tasks — SQLite
connections are not safe to use concurrently, and short-lived ones sidestep the
question entirely.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .config import settings

SCHEMA = """
-- Step 2 cache: one airport picture, always sourced from exactly one provider.
CREATE TABLE IF NOT EXISTS airport_schedule_cache (
    iata            TEXT PRIMARY KEY,
    fetched_at      INTEGER NOT NULL,
    source_provider TEXT NOT NULL,
    flight_count    INTEGER NOT NULL,
    window_start    TEXT NOT NULL,
    window_end      TEXT NOT NULL,
    raw_payload     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flights (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    iata              TEXT NOT NULL,
    flight_iata       TEXT,
    airline_iata      TEXT,
    dep_terminal      TEXT,
    dep_terminal_norm TEXT,
    dep_time_local    TEXT NOT NULL,
    dep_time_utc      TEXT,
    status            TEXT,
    source_provider   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flights_lookup ON flights(iata, dep_time_local);
CREATE INDEX IF NOT EXISTS idx_flights_terminal ON flights(iata, dep_terminal_norm);

-- Step 1 cache. recheck_after carries the dynamic TTL: there is no point
-- re-asking for a terminal before the airline has assigned one.
CREATE TABLE IF NOT EXISTS flight_resolution_cache (
    flight_no       TEXT NOT NULL,
    flight_date     TEXT NOT NULL,
    dep_iata        TEXT,
    dep_terminal    TEXT,
    dep_time_local  TEXT,
    arr_iata        TEXT,
    resolved_at     INTEGER NOT NULL,
    recheck_after   INTEGER NOT NULL,
    source_provider TEXT NOT NULL,
    payload         TEXT,
    PRIMARY KEY (flight_no, flight_date)
);

-- Corpus: both tables are populated as a side effect of normal fetches, so
-- they cost no additional API budget.
CREATE TABLE IF NOT EXISTS flight_terminal_history (
    flight_iata    TEXT NOT NULL,
    terminal_norm  TEXT NOT NULL,
    observed_count INTEGER NOT NULL DEFAULT 1,
    last_seen      INTEGER NOT NULL,
    PRIMARY KEY (flight_iata, terminal_norm)
);

CREATE TABLE IF NOT EXISTS terminal_density_history (
    iata          TEXT NOT NULL,
    terminal_norm TEXT NOT NULL,   -- '*' is the whole-airport aggregate
    day_of_week   INTEGER NOT NULL,
    hour          INTEGER NOT NULL,
    avg_flights   REAL NOT NULL,
    sample_count  INTEGER NOT NULL,
    -- Boards are refetched every 4h and each covers 12h, so the same calendar
    -- hour arrives repeatedly. Recording the source date lets a re-observation
    -- be skipped, keeping sample_count an honest count of distinct days.
    last_sample_date TEXT,
    PRIMARY KEY (iata, terminal_norm, day_of_week, hour)
);

-- Budget ledger. Every attempted provider call lands here, including blocked
-- ones, so the record explains its own decisions after the fact.
CREATE TABLE IF NOT EXISTS api_usage (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    provider  TEXT NOT NULL,
    endpoint  TEXT NOT NULL,
    called_at INTEGER NOT NULL,
    day_key   TEXT NOT NULL,
    month_key TEXT NOT NULL,
    status    TEXT NOT NULL,
    -- One operation can spend several calls: AirLabs pages through a large
    -- airport. Storing the count keeps the ledger honest about what a single
    -- logical request actually cost.
    calls     INTEGER NOT NULL DEFAULT 1,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_month ON api_usage(month_key, provider, status);
CREATE INDEX IF NOT EXISTS idx_usage_day ON api_usage(day_key, status);

-- Router state is persisted so a crash loop cannot re-probe providers already
-- known to be exhausted, which would burn the pooled budget on restarts.
CREATE TABLE IF NOT EXISTS provider_state (
    provider      TEXT PRIMARY KEY,
    state         TEXT NOT NULL,
    state_until   INTEGER,
    month_key     TEXT,
    last_error    TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL
);
"""

# Columns added after the first release. This app is upgraded in place with
# `git pull` on the GPD, so the database outlives any single version of the
# schema and additive changes have to be applied on startup.
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("api_usage", "calls", "INTEGER NOT NULL DEFAULT 1"),
    ("provider_state", "failure_count", "INTEGER NOT NULL DEFAULT 0"),
    ("terminal_density_history", "last_sample_date", "TEXT"),
    # Budget as the provider itself reports it. AeroDataBox meters "API units"
    # that do not map one-to-one onto requests, so counting calls locally
    # measures the wrong quantity; these are authoritative.
    ("provider_state", "units_remaining", "INTEGER"),
    ("provider_state", "units_limit", "INTEGER"),
    ("provider_state", "requests_remaining", "INTEGER"),
    ("provider_state", "quota_synced_at", "INTEGER"),
]


def _db_file() -> Path:
    path = settings.resolved_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection; commit on success, roll back on error."""
    conn = sqlite3.connect(_db_file(), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _apply_added_columns(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _apply_added_columns(conn)


# --- Time keys -------------------------------------------------------------
# Budget windows are keyed on the server's local calendar, not UTC. The month
# boundary only has to be self-consistent, and local dates are what the
# operator sees when reading /api/quota.

def day_key(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m-%d")


def month_key(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime("%Y-%m")


def now_ts() -> int:
    return int(datetime.now().timestamp())
