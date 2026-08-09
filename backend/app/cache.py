"""Step 2: the airport departure board, cached.

The expensive operation in this app is fetching a board, so nearly all of the
cost control lives here.

**Per-airport locks with a re-check after acquiring.** Five friends asking
about SFO within the same second must cost one call, not five. The re-check
matters as much as the lock: without it, every waiter proceeds to fetch the
data the first one just stored.

**Degradation is layered, never a hard stop.** When the budget is spent or a
provider fails, a stale board is served with its age reported rather than
returning an error. Someone mid-trip is better served by a four-hour-old board,
clearly labelled, than by a failure — and the caller can still fall back to the
historical baseline beyond that.

**The horizon check spends nothing to learn nothing.** No provider publishes a
departure board weeks ahead, so a request past `board_horizon_days` skips the
call entirely rather than paying to discover an empty result.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import budget, db
from .config import settings
from .normalize import NormalizedFlight, format_local_time, parse_local_time
from .router import AllProvidersUnavailable, ProviderRouter

log = logging.getLogger(__name__)

# Sources, in descending order of trustworthiness.
FRESH = "fresh"      # just fetched
CACHE = "cache"      # within TTL
STALE = "stale"      # past TTL, served because the alternative was nothing
NONE = "none"        # no board at all

_locks: dict[str, asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _lock_for(iata: str) -> asyncio.Lock:
    async with _locks_guard:
        if iata not in _locks:
            _locks[iata] = asyncio.Lock()
        return _locks[iata]


@dataclass
class BoardResult:
    iata: str
    flights: list[NormalizedFlight]
    data_source: str
    calls_used: int = 0
    cache_age_minutes: int | None = None
    source_provider: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    note: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.flights

    def covers(self, start: datetime, end: datetime) -> bool:
        """Whether the cached window actually spans the range being asked about.

        A board can be fresh and still useless: fetched for this morning when
        the question is about tonight.
        """
        if self.window_start is None or self.window_end is None:
            return False
        return self.window_start <= start and end <= self.window_end


def plan_window(departure: datetime) -> tuple[datetime, datetime]:
    """Choose the block to fetch around a departure.

    Centred rather than ending at the departure, so one fetch also answers
    nearby questions from other users at the same airport.
    """
    block = timedelta(hours=settings.departure_block_hours)
    start = (departure - block / 2).replace(minute=0, second=0, microsecond=0)
    return start, start + block


def beyond_horizon(departure: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return departure - now > timedelta(days=settings.board_horizon_days)


# --- Persistence ------------------------------------------------------------

def load_board(iata: str) -> BoardResult:
    iata = iata.upper()
    with db.connect() as conn:
        meta = conn.execute(
            "SELECT * FROM airport_schedule_cache WHERE iata = ?", (iata,)
        ).fetchone()
        if meta is None:
            return BoardResult(iata=iata, flights=[], data_source=NONE)

        rows = conn.execute(
            "SELECT * FROM flights WHERE iata = ? ORDER BY dep_time_local", (iata,)
        ).fetchall()

    flights = []
    for r in rows:
        dep = parse_local_time(r["dep_time_local"])
        if dep is None:
            continue
        flights.append(
            NormalizedFlight(
                dep_iata=r["iata"],
                dep_time_local=dep,
                flight_iata=r["flight_iata"],
                airline_iata=r["airline_iata"],
                dep_terminal=r["dep_terminal"],
                dep_terminal_norm=r["dep_terminal_norm"],
                dep_time_utc=r["dep_time_utc"],
                status=r["status"],
                source_provider=r["source_provider"],
            )
        )

    age = max(0, int((db.now_ts() - meta["fetched_at"]) / 60))
    fresh = age < settings.cache_ttl_hours * 60

    return BoardResult(
        iata=iata,
        flights=flights,
        data_source=CACHE if fresh else STALE,
        cache_age_minutes=age,
        source_provider=meta["source_provider"],
        window_start=parse_local_time(meta["window_start"]),
        window_end=parse_local_time(meta["window_end"]),
    )


def store_board(
    iata: str,
    flights: list[NormalizedFlight],
    window_start: datetime,
    window_end: datetime,
    source_provider: str,
    raw_payload,
) -> None:
    """Replace an airport's board atomically.

    Delete-then-insert inside one transaction: the table must never hold a mix
    of two fetches, and a single cached picture must come from exactly one
    provider. Mixing providers would silently corrupt the terminal filter,
    since one may report terminals where another reports concourses.
    """
    iata = iata.upper()
    with db.connect() as conn:
        conn.execute("DELETE FROM flights WHERE iata = ?", (iata,))
        conn.executemany(
            """
            INSERT INTO flights
                (iata, flight_iata, airline_iata, dep_terminal, dep_terminal_norm,
                 dep_time_local, dep_time_utc, status, source_provider)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    iata,
                    f.flight_iata,
                    f.airline_iata,
                    f.dep_terminal,
                    f.dep_terminal_norm,
                    format_local_time(f.dep_time_local),
                    f.dep_time_utc,
                    f.status,
                    f.source_provider,
                )
                for f in flights
            ],
        )
        conn.execute(
            """
            INSERT INTO airport_schedule_cache
                (iata, fetched_at, source_provider, flight_count,
                 window_start, window_end, raw_payload)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(iata) DO UPDATE SET
                fetched_at = excluded.fetched_at,
                source_provider = excluded.source_provider,
                flight_count = excluded.flight_count,
                window_start = excluded.window_start,
                window_end = excluded.window_end,
                raw_payload = excluded.raw_payload
            """,
            (
                iata,
                db.now_ts(),
                source_provider,
                len(flights),
                format_local_time(window_start),
                format_local_time(window_end),
                # Retained so a mapping correction can be applied to existing
                # data without spending budget to re-fetch.
                json.dumps(raw_payload, default=str)[:1_000_000],
            ),
        )


# --- The cache-or-fetch path ------------------------------------------------

async def get_board(
    router: ProviderRouter,
    iata: str,
    departure: datetime,
    *,
    now: datetime | None = None,
) -> BoardResult:
    now = now or datetime.now()
    iata = iata.upper()
    rush_start = departure - timedelta(hours=settings.rush_window_hours)

    existing = load_board(iata)
    if existing.data_source == CACHE and existing.covers(rush_start, departure):
        return existing  # free: no lock, no ledger, no call

    if beyond_horizon(departure, now):
        # Nothing to buy. Hand back whatever exists so the caller can fall
        # through to the historical baseline.
        existing.note = (
            f"departure is more than {settings.board_horizon_days} days out; "
            "no live board exists that far ahead"
        )
        return existing

    lock = await _lock_for(iata)
    async with lock:
        # Re-check: a concurrent request may have just fetched this board.
        # Without this, every waiter refetches what the first one stored.
        existing = load_board(iata)
        if existing.data_source == CACHE and existing.covers(rush_start, departure):
            return existing

        window_start, window_end = plan_window(departure)
        try:
            result = await router.fetch_departures(iata, window_start, window_end)
        except AllProvidersUnavailable as exc:
            return _degrade(existing, iata, str(exc))

        store_board(
            iata,
            result.flights,
            window_start,
            window_end,
            result.provider,
            result.raw,
        )
        return BoardResult(
            iata=iata,
            flights=result.flights,
            data_source=FRESH,
            calls_used=result.calls_used,
            cache_age_minutes=0,
            source_provider=result.provider,
            window_start=window_start,
            window_end=window_end,
            note=result.partial_reason,
        )


def _degrade(existing: BoardResult, iata: str, reason: str) -> BoardResult:
    """Serve what we have rather than failing outright."""
    if existing.flights:
        log.info("serving stale board for %s: %s", iata, reason)
        existing.data_source = STALE
        existing.note = f"live data unavailable ({reason}); showing cached data"
        return existing

    log.warning("no board available for %s: %s", iata, reason)
    return BoardResult(iata=iata, flights=[], data_source=NONE, note=reason)


def count_terminals(iata: str) -> int:
    """Distinct terminals seen in the cached board.

    Used to scale checkpoint capacity when a prediction falls back to
    whole-airport scope — see predict.py, where getting this wrong makes every
    fallback report "Severe".
    """
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT dep_terminal_norm) AS n FROM flights
            WHERE iata = ? AND dep_terminal_norm IS NOT NULL
            """,
            (iata.upper(),),
        ).fetchone()
    return int(row["n"] or 0)
