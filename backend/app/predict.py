"""The prediction itself: departure density -> a security wait estimate.

Two things here are worth reading before changing anything.

**Demand is converted to a rate before comparison.** The original spec compares
estimated passengers — accumulated over a *2-hour* window — against a *1-hour*
capacity of 750. That would double every result. Passengers are divided by the
window length first, so a per-hour demand meets a per-hour capacity. The raw
2-hour total is still reported, as the spec requires.

**Capacity scales with scope.** When a prediction falls back from one terminal
to the whole airport, demand widens to every departure at the airport. If
capacity stayed at a single terminal's 750/hour, the ratio would be inflated by
roughly the number of terminals and the answer would be "Severe" for
essentially every fallback — confidently wrong exactly when confidence is
lowest. `capacity_for()` scales with the terminal count observed in the board,
and a test asserts a whole-airport fallback does not read Severe on a normal
day.

The output is a heuristic, not a model fitted to observed wait times. Every
result carries its assumptions so the UI can show its work, and the
recommended arrival time includes a gate buffer on top of the estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import history
from .cache import (
    BoardResult,
    count_terminals,
    peak_hourly_departures,
    required_span,
)
from .config import settings
from .normalize import NormalizedFlight

# Scope of the calculation.
TERMINAL = "terminal"
AIRPORT = "airport"

# How much to trust the answer — surfaced in the UI, never hidden.
HIGH = "high"
MEDIUM = "medium"
LOW = "low"

LIGHT = "Light"
MODERATE = "Moderate"
SEVERE = "Severe"

# What the estimate was computed from.
LIVE = "live"
BASELINE = "baseline"


class PredictionUnavailable(Exception):
    """Ladder level 5: no live board and no usable history."""

    def __init__(self, message: str, code: str = "out_of_range") -> None:
        self.code = code
        super().__init__(message)


@dataclass
class Prediction:
    airport: str
    terminal: str | None
    scope: str
    confidence: str
    confidence_reason: str
    basis: str
    rush_window_start: datetime
    rush_window_end: datetime
    flights_in_window: float
    estimated_passengers: int
    demand_per_hour: int
    capacity_per_hour: int
    load_ratio: float
    wait_category: str
    estimated_wait_minutes: int
    recommended_arrival_local: datetime
    terminal_matched: bool
    assumptions: dict = field(default_factory=dict)
    note: str | None = None


# --- The arithmetic ---------------------------------------------------------

def co_queue_weight(flight_departure: datetime, your_departure: datetime) -> float:
    """How much a given flight's passengers share the queue with you.

    Counting only flights that depart *before* yours measures the wrong crowd:
    those passengers are largely through security already. What matters is
    whose security window overlaps yours, and passengers for a flight leaving
    at D are at security roughly [D - lead_max, D - lead_min].

    Two such windows overlap by `W - |D1 - D2|`, so the share collapses to a
    triangular weight: a flight leaving alongside yours counts fully, one
    leaving W either side counts nothing, and — the point — a flight leaving
    *after* yours counts just as much as one leaving the same interval before.
    """
    window = settings.security_window_hours
    if window <= 0:
        return 0.0
    delta_hours = abs((flight_departure - your_departure).total_seconds()) / 3600.0
    return max(0.0, 1.0 - delta_hours / window)


def security_window(departure: datetime) -> tuple[datetime, datetime]:
    """When you are physically at security."""
    return (
        departure - timedelta(minutes=settings.security_lead_max_minutes),
        departure - timedelta(minutes=settings.security_lead_min_minutes),
    )


def counted_window(departure: datetime) -> tuple[datetime, datetime]:
    """The span of departures whose passengers can share your queue.

    Delegates to cache.required_span so the cache's coverage check and this
    one cannot drift apart — when they did, a board passed the cache check and
    was then rejected here, surfacing as a spurious "no schedule available".
    """
    return required_span(departure)


def estimate_lanes(
    iata: str, scope: str, terminal_norm: str | None, n_terminals: int
) -> tuple[int, str]:
    """Infer how many security lanes serve this scope.

    Lane counts are not published in any usable form — TSA does not release
    them, airport sites are inconsistent, and non-US airports have nothing
    comparable. The best available proxy is the airport's own busiest hour:
    whatever it was built to handle, it was built to handle that.

    Sizing on the *peak* while measuring demand hour by hour is what keeps the
    output meaningful. Sizing on current demand instead would make the ratio
    roughly constant everywhere, and every airport would read the same at every
    hour.
    """
    if not settings.estimate_lanes:
        fallback = settings.lanes_per_terminal * (
            1 if scope == TERMINAL else max(n_terminals, 1)
        )
        return fallback, "configured"

    scope_terminal = terminal_norm if scope == TERMINAL else None

    # The corpus knows the true daily peak; the board only covers 12 hours and
    # can miss it, which would undersize the checkpoint and overstate the wait.
    peak = history.peak_hourly_departures(iata, scope_terminal)
    source = "estimated from history"
    if peak is None:
        peak = float(peak_hourly_departures(iata, scope_terminal))
        source = "estimated from today's schedule"

    if peak <= 0:
        fallback = settings.lanes_per_terminal * (
            1 if scope == TERMINAL else max(n_terminals, 1)
        )
        return fallback, "default (no schedule seen)"

    peak_passengers = peak * settings.seats_per_flight * settings.origin_pax_factor
    lanes = round(
        peak_passengers / settings.pax_per_lane_per_hour * settings.lane_design_factor
    )
    lanes = max(settings.min_estimated_lanes, min(settings.max_estimated_lanes, lanes))
    return int(lanes), source


def capacity_for(scope: str, n_terminals: int) -> int:
    """Fixed-lane capacity. Retained for the configured (non-estimating) path."""
    base = settings.terminal_capacity_per_hour
    if scope == TERMINAL:
        return base
    return base * max(n_terminals, 1)


def estimate_passengers(flight_count: float) -> int:
    return int(round(flight_count * settings.seats_per_flight * settings.origin_pax_factor))


def categorize(ratio: float) -> str:
    if ratio < settings.light_max_ratio:
        return LIGHT
    if ratio <= settings.moderate_max_ratio:
        return MODERATE
    return SEVERE


def estimate_wait_minutes(passengers: int, capacity_per_hour: int) -> int:
    """Queue wait, in minutes.

    Below saturation the wait grows smoothly with utilization. Above it, demand
    exceeds throughput and a backlog forms, so the excess is divided by the
    service rate and added — which is why the curve steepens past ratio 1.
    """
    window_capacity = capacity_per_hour * settings.security_window_hours
    if window_capacity <= 0:
        return int(settings.max_wait_min)

    ratio = (passengers / settings.security_window_hours) / capacity_per_hour

    if ratio <= 1.0:
        wait = settings.base_wait_min + (
            settings.saturated_wait_min - settings.base_wait_min
        ) * ratio
    else:
        backlog = passengers - window_capacity
        wait = settings.saturated_wait_min + backlog / (capacity_per_hour / 60.0)

    return int(round(min(wait, settings.max_wait_min)))


def _in_scope(
    flights: list[NormalizedFlight], terminal_norm: str | None
) -> list[NormalizedFlight]:
    if terminal_norm is None:
        return flights
    return [f for f in flights if f.dep_terminal_norm == terminal_norm]


def _effective_flights(
    flights: list[NormalizedFlight], departure: datetime, terminal_norm: str | None
) -> float:
    """Flights weighted by how much their crowd overlaps yours.

    A fractional result is correct rather than sloppy: a flight leaving 60
    minutes after yours genuinely contributes about half its passengers to the
    queue you stand in.
    """
    return sum(
        co_queue_weight(f.dep_time_local, departure)
        for f in _in_scope(flights, terminal_norm)
    )


def _assumptions() -> dict:
    return {
        "seats_per_flight": settings.seats_per_flight,
        "origin_passenger_factor": settings.origin_pax_factor,
        "lanes_per_terminal": settings.lanes_per_terminal,
        "passengers_per_lane_per_hour": settings.pax_per_lane_per_hour,
        "security_window_hours": settings.security_window_hours,
        "gate_buffer_minutes": settings.gate_buffer_min,
    }


def _build(
    *,
    airport: str,
    terminal: str | None,
    scope: str,
    confidence: str,
    reason: str,
    basis: str,
    window_start: datetime,
    window_end: datetime,
    departure: datetime,
    flight_count: float,
    n_terminals: int,
    terminal_matched: bool,
    airport_code: str | None = None,
    terminal_norm: str | None = None,
    note: str | None = None,
) -> Prediction:
    passengers = estimate_passengers(flight_count)
    lanes, lanes_source = estimate_lanes(
        airport_code or airport, scope, terminal_norm, n_terminals
    )
    capacity = lanes * settings.pax_per_lane_per_hour
    demand_per_hour = passengers / settings.security_window_hours
    ratio = demand_per_hour / capacity if capacity else 0.0
    wait = estimate_wait_minutes(passengers, capacity)

    return Prediction(
        airport=airport,
        terminal=terminal,
        scope=scope,
        confidence=confidence,
        confidence_reason=reason,
        basis=basis,
        rush_window_start=window_start,
        rush_window_end=window_end,
        flights_in_window=round(flight_count, 1),
        estimated_passengers=passengers,
        demand_per_hour=int(round(demand_per_hour)),
        capacity_per_hour=capacity,
        load_ratio=round(ratio, 2),
        wait_category=categorize(ratio),
        estimated_wait_minutes=wait,
        recommended_arrival_local=departure
        - timedelta(minutes=wait + settings.gate_buffer_min),
        terminal_matched=terminal_matched,
        assumptions={**_assumptions(), "lanes": lanes, "lanes_source": lanes_source},
        note=note,
    )


# --- The fallback ladder ----------------------------------------------------

def predict(
    board: BoardResult,
    departure: datetime,
    *,
    terminal_norm: str | None = None,
    flight_iata: str | None = None,
) -> Prediction:
    """Predict the wait, degrading through the ladder in BUILD_PLAN.md §6.

    Levels are tried in order and the outcome is always reported, never
    silently applied:

    1. terminal reported            -> terminal scope, high
    2. terminal inferred from history -> terminal scope, medium
    3. no terminal                  -> airport scope (capacity scaled), low
    4. no live board                -> historical baseline, low
    5. neither                      -> PredictionUnavailable
    """
    airport = board.iata.upper()
    # Report the window the user is actually at security; count the wider span
    # of departures whose crowds overlap it.
    window_start, window_end = security_window(departure)
    counted_start, counted_end = counted_window(departure)
    n_terminals = count_terminals(airport)

    # Coverage is judged against the counted span, since flights after the
    # departure now contribute too.
    board_covers = board.flights and board.covers(counted_start, counted_end)

    # --- Levels 1-3: a live board exists ---
    if board_covers:
        if terminal_norm is not None:
            weighted = _effective_flights(board.flights, departure, terminal_norm)
            if weighted > 0:
                return _build(
                    airport=airport, terminal=terminal_norm, scope=TERMINAL,
                    confidence=HIGH, reason="Terminal reported by the schedule",
                    basis=LIVE, window_start=window_start, window_end=window_end,
                    departure=departure, flight_count=weighted,
                    n_terminals=n_terminals, terminal_matched=True,
                    airport_code=airport, terminal_norm=terminal_norm,
                    note=board.note,
                )
            # Terminal given but nothing matched it — the board disagrees with
            # the resolution, so widening is more honest than reporting zero.
            return _airport_scope(
                board, departure, window_start, window_end, n_terminals, airport,
                reason=(
                    f"No departures found for Terminal {terminal_norm}; "
                    "estimating for the whole airport"
                ),
                note=board.note,
            )

        guess = history.modal_terminal(flight_iata)
        if guess:
            weighted = _effective_flights(board.flights, departure, guess.terminal)
            if weighted > 0:
                return _build(
                    airport=airport, terminal=guess.terminal, scope=TERMINAL,
                    confidence=MEDIUM,
                    reason=f"Terminal not published yet; {flight_iata} {guess.confidence_text}",
                    basis=LIVE, window_start=window_start, window_end=window_end,
                    departure=departure, flight_count=weighted,
                    n_terminals=n_terminals, terminal_matched=False,
                    airport_code=airport, terminal_norm=guess.terminal,
                    note=board.note,
                )

        return _airport_scope(
            board, departure, window_start, window_end, n_terminals, airport,
            reason="Terminal not published yet; estimating for the whole airport",
            note=board.note,
        )

    # --- Level 4: no usable board, fall back to accumulated history ---
    baseline_terminal = terminal_norm
    if baseline_terminal is None:
        guess = history.modal_terminal(flight_iata)
        baseline_terminal = guess.terminal if guess else None

    estimate = history.baseline_for_window(
        airport, baseline_terminal, window_start, window_end
    )
    if estimate is None and baseline_terminal is not None:
        estimate = history.baseline_for_window(airport, None, window_start, window_end)
        baseline_terminal = None

    if estimate is not None:
        scope = TERMINAL if baseline_terminal else AIRPORT
        return _build(
            airport=airport, terminal=baseline_terminal, scope=scope,
            confidence=LOW,
            reason=(
                f"No live schedule for this date; {estimate.description}, "
                "not today's actual flights"
            ),
            basis=BASELINE, window_start=window_start, window_end=window_end,
            departure=departure, flight_count=estimate.flights,
            n_terminals=n_terminals, terminal_matched=bool(baseline_terminal),
            airport_code=airport, terminal_norm=baseline_terminal,
            note=board.note,
        )

    # --- Level 5 ---
    # Say why *and* what to do. The board's own note explains the cause
    # precisely ("more than 7 days out"), but on its own it leaves the user
    # with no next step.
    cause = board.note or "No departure schedule is published for that date yet"
    raise PredictionUnavailable(
        f"{cause}, and there is not enough history for {airport} to estimate "
        "from yet. Check back closer to your flight."
    )


def _airport_scope(
    board: BoardResult,
    departure: datetime,
    window_start: datetime,
    window_end: datetime,
    n_terminals: int,
    airport: str,
    *,
    reason: str,
    note: str | None,
) -> Prediction:
    weighted = _effective_flights(board.flights, departure, None)
    return _build(
        airport=airport, terminal=None, scope=AIRPORT, confidence=LOW,
        reason=reason, basis=LIVE, window_start=window_start,
        window_end=window_end, departure=departure, flight_count=weighted,
        n_terminals=n_terminals, terminal_matched=False,
        airport_code=airport, note=note,
    )
