"""Step 1: flight number + date -> departure airport, terminal, and time.

This step is cached, and that is not an optimization detail. Uncached, resolving
"UA123" costs a call on *every* query — five friends on the same flight, or one
friend checking five times, is five calls even when the airport board is a
perfect cache hit. Caching roughly halves total spend.

**The TTL is dynamic**, which is what turns the terminal-null problem from an
edge case into a scheduling rule. Airlines do not assign terminals until
roughly 48-72h before departure, so re-asking before then spends budget on a
question the provider cannot answer. Instead the re-check is *scheduled* for
the moment the answer can first exist.

Multi-leg flights are returned as a list rather than silently collapsed to the
first. One flight number can cover several legs on the same date, and guessing
would strand a user at the wrong airport.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db
from .normalize import (
    FlightResolution,
    format_local_time,
    normalize_terminal,
    parse_flight_number,
    parse_local_time,
)
from .router import ProviderRouter

log = logging.getLogger(__name__)

# Terminals are typically published 48-72h out.
TERMINAL_AVAILABLE_HOURS = 48
TERMINAL_DEAD_ZONE_HOURS = 72

# Never schedule a re-check sooner than this; without a floor, a departure
# already inside the re-check offset would refetch on every single request.
MIN_RECHECK_SECONDS = 1800          # 30 min
NEAR_DEPARTURE_RECHECK_SECONDS = 3600      # 1h once a terminal is known
NO_TERMINAL_RECHECK_SECONDS = 6 * 3600     # 6h inside the assignment window
PAST_FLIGHT_RECHECK_SECONDS = 24 * 3600    # departed; nothing will change


class InvalidFlightNumber(ValueError):
    def __init__(self, raw: str | None) -> None:
        self.raw = raw
        super().__init__(
            f"{raw!r} is not a valid flight number (expected e.g. UA123)"
        )


@dataclass
class ResolutionOutcome:
    resolutions: list[FlightResolution]
    from_cache: bool
    calls_used: int
    source_provider: str

    @property
    def ambiguous(self) -> bool:
        """More than one leg on this date — the user has to choose."""
        return len(self.resolutions) > 1

    @property
    def single(self) -> FlightResolution:
        return self.resolutions[0]


def compute_recheck_after(
    departure: datetime | None,
    terminal_norm: str | None,
    now: datetime | None = None,
) -> int:
    """When it is next worth spending a call on this flight.

    The dead-zone branch is the important one: for a departure weeks away, the
    re-check is scheduled for 48h before it, so the intervening weeks cost
    nothing no matter how often the flight is looked up.
    """
    now = now or datetime.now()

    if departure is None:
        return int((now + timedelta(seconds=NO_TERMINAL_RECHECK_SECONDS)).timestamp())

    if departure < now:
        return int((now + timedelta(seconds=PAST_FLIGHT_RECHECK_SECONDS)).timestamp())

    if terminal_norm is not None:
        # Known, but terminals do get reassigned close in.
        target = max(
            now + timedelta(seconds=NEAR_DEPARTURE_RECHECK_SECONDS),
            departure - timedelta(hours=6),
        )
    elif departure - now > timedelta(hours=TERMINAL_DEAD_ZONE_HOURS):
        # No terminal can exist yet. Wait until the first moment one might.
        target = departure - timedelta(hours=TERMINAL_AVAILABLE_HOURS)
    else:
        # Inside the assignment window but still unassigned; poll gently.
        target = now + timedelta(seconds=NO_TERMINAL_RECHECK_SECONDS)

    floor = now + timedelta(seconds=MIN_RECHECK_SECONDS)
    return int(max(target, floor).timestamp())


def _load_cached(flight_no: str, flight_date: str, now: datetime) -> list[FlightResolution] | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM flight_resolution_cache WHERE flight_no = ? AND flight_date = ?",
            (flight_no, flight_date),
        ).fetchone()

    if row is None or row["recheck_after"] <= int(now.timestamp()):
        return None

    payload = json.loads(row["payload"]) if row["payload"] else []
    if not payload:
        return None

    return [
        FlightResolution(
            flight_no=flight_no,
            flight_date=flight_date,
            dep_iata=leg.get("dep_iata"),
            dep_terminal=leg.get("dep_terminal"),
            dep_terminal_norm=normalize_terminal(leg.get("dep_terminal")),
            dep_time_local=parse_local_time(leg.get("dep_time_local")),
            arr_iata=leg.get("arr_iata"),
            source_provider=row["source_provider"],
            extra={"dep_airport_name": leg.get("dep_airport_name")},
        )
        for leg in payload
    ]


def _store(resolutions: list[FlightResolution], now: datetime) -> None:
    first = resolutions[0]
    payload = json.dumps(
        [
            {
                "dep_iata": r.dep_iata,
                "dep_terminal": r.dep_terminal,
                "dep_time_local": (
                    format_local_time(r.dep_time_local) if r.dep_time_local else None
                ),
                "arr_iata": r.arr_iata,
                "dep_airport_name": r.extra.get("dep_airport_name"),
            }
            for r in resolutions
        ]
    )

    # For a multi-leg flight the re-check is driven by the earliest departure:
    # whichever leg the user picks, the cache must refresh in time for it.
    earliest = min(
        (r.dep_time_local for r in resolutions if r.dep_time_local), default=None
    )
    any_terminal = next(
        (r.dep_terminal_norm for r in resolutions if r.dep_terminal_norm), None
    )
    recheck = compute_recheck_after(earliest, any_terminal, now)

    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO flight_resolution_cache
                (flight_no, flight_date, dep_iata, dep_terminal, dep_time_local,
                 arr_iata, resolved_at, recheck_after, source_provider, payload)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(flight_no, flight_date) DO UPDATE SET
                dep_iata = excluded.dep_iata,
                dep_terminal = excluded.dep_terminal,
                dep_time_local = excluded.dep_time_local,
                arr_iata = excluded.arr_iata,
                resolved_at = excluded.resolved_at,
                recheck_after = excluded.recheck_after,
                source_provider = excluded.source_provider,
                payload = excluded.payload
            """,
            (
                first.flight_no,
                first.flight_date,
                first.dep_iata,
                first.dep_terminal,
                format_local_time(first.dep_time_local) if first.dep_time_local else None,
                first.arr_iata,
                int(now.timestamp()),
                recheck,
                first.source_provider,
                payload,
            ),
        )


async def resolve(
    router: ProviderRouter,
    flight_no: str,
    flight_date: str,
    *,
    prefer_iata: str | None = None,
    now: datetime | None = None,
) -> ResolutionOutcome:
    """Resolve a flight, preferring cache.

    `prefer_iata` narrows a previously-returned multi-leg result to the leg the
    user picked. It is applied to the cached legs, so disambiguation costs no
    additional call.
    """
    now = now or datetime.now()

    canonical = parse_flight_number(flight_no)
    if not canonical:
        # Rejected locally: a malformed number must never spend budget.
        raise InvalidFlightNumber(flight_no)

    cached = _load_cached(canonical, flight_date, now)
    if cached:
        return ResolutionOutcome(
            resolutions=_narrow(cached, prefer_iata),
            from_cache=True,
            calls_used=0,
            source_provider=cached[0].source_provider,
        )

    result = await router.resolve_flight(canonical, flight_date)
    resolutions = [r for r in result.resolutions if r.dep_iata]
    if resolutions:
        _store(resolutions, now)

    return ResolutionOutcome(
        resolutions=_narrow(resolutions, prefer_iata),
        from_cache=False,
        calls_used=result.calls_used,
        source_provider=result.provider,
    )


def _narrow(
    resolutions: list[FlightResolution], prefer_iata: str | None
) -> list[FlightResolution]:
    if not prefer_iata:
        return resolutions
    wanted = prefer_iata.strip().upper()
    matched = [r for r in resolutions if r.dep_iata == wanted]
    return matched or resolutions
