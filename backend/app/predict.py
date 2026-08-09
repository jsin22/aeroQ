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
from .cache import BoardResult, count_terminals
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

def capacity_for(scope: str, n_terminals: int) -> int:
    """Checkpoint throughput per hour for the scope being measured.

    The whole-airport branch is the v1 bug fix: widening demand to the airport
    without widening capacity made every fallback report Severe.
    """
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
    window_capacity = capacity_per_hour * settings.rush_window_hours
    if window_capacity <= 0:
        return int(settings.max_wait_min)

    ratio = (passengers / settings.rush_window_hours) / capacity_per_hour

    if ratio <= 1.0:
        wait = settings.base_wait_min + (
            settings.saturated_wait_min - settings.base_wait_min
        ) * ratio
    else:
        backlog = passengers - window_capacity
        wait = settings.saturated_wait_min + backlog / (capacity_per_hour / 60.0)

    return int(round(min(wait, settings.max_wait_min)))


def _filter_window(
    flights: list[NormalizedFlight],
    start: datetime,
    end: datetime,
    terminal_norm: str | None,
) -> list[NormalizedFlight]:
    out = [f for f in flights if start <= f.dep_time_local < end]
    if terminal_norm is not None:
        out = [f for f in out if f.dep_terminal_norm == terminal_norm]
    return out


def _assumptions() -> dict:
    return {
        "seats_per_flight": settings.seats_per_flight,
        "origin_passenger_factor": settings.origin_pax_factor,
        "lanes_per_terminal": settings.lanes_per_terminal,
        "passengers_per_lane_per_hour": settings.pax_per_lane_per_hour,
        "rush_window_hours": settings.rush_window_hours,
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
    note: str | None = None,
) -> Prediction:
    passengers = estimate_passengers(flight_count)
    capacity = capacity_for(scope, n_terminals)
    demand_per_hour = passengers / settings.rush_window_hours
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
        assumptions=_assumptions(),
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
    window_start = departure - timedelta(hours=settings.rush_window_hours)
    n_terminals = count_terminals(airport)

    board_covers = board.flights and board.covers(window_start, departure)

    # --- Levels 1-3: a live board exists ---
    if board_covers:
        if terminal_norm is not None:
            matched = _filter_window(board.flights, window_start, departure, terminal_norm)
            if matched:
                return _build(
                    airport=airport, terminal=terminal_norm, scope=TERMINAL,
                    confidence=HIGH, reason="Terminal reported by the schedule",
                    basis=LIVE, window_start=window_start, window_end=departure,
                    departure=departure, flight_count=len(matched),
                    n_terminals=n_terminals, terminal_matched=True,
                    note=board.note,
                )
            # Terminal given but nothing matched it — the board disagrees with
            # the resolution, so widening is more honest than reporting zero.
            return _airport_scope(
                board, departure, window_start, n_terminals, airport,
                reason=(
                    f"No departures found for Terminal {terminal_norm}; "
                    "estimating for the whole airport"
                ),
                note=board.note,
            )

        guess = history.modal_terminal(flight_iata)
        if guess:
            matched = _filter_window(
                board.flights, window_start, departure, guess.terminal
            )
            if matched:
                return _build(
                    airport=airport, terminal=guess.terminal, scope=TERMINAL,
                    confidence=MEDIUM,
                    reason=f"Terminal not published yet; {flight_iata} {guess.confidence_text}",
                    basis=LIVE, window_start=window_start, window_end=departure,
                    departure=departure, flight_count=len(matched),
                    n_terminals=n_terminals, terminal_matched=False,
                    note=board.note,
                )

        return _airport_scope(
            board, departure, window_start, n_terminals, airport,
            reason="Terminal not published yet; estimating for the whole airport",
            note=board.note,
        )

    # --- Level 4: no usable board, fall back to accumulated history ---
    baseline_terminal = terminal_norm
    if baseline_terminal is None:
        guess = history.modal_terminal(flight_iata)
        baseline_terminal = guess.terminal if guess else None

    estimate = history.baseline_for_window(
        airport, baseline_terminal, window_start, departure
    )
    if estimate is None and baseline_terminal is not None:
        estimate = history.baseline_for_window(airport, None, window_start, departure)
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
            basis=BASELINE, window_start=window_start, window_end=departure,
            departure=departure, flight_count=estimate.flights,
            n_terminals=n_terminals, terminal_matched=bool(baseline_terminal),
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
    n_terminals: int,
    airport: str,
    *,
    reason: str,
    note: str | None,
) -> Prediction:
    everything = _filter_window(board.flights, window_start, departure, None)
    return _build(
        airport=airport, terminal=None, scope=AIRPORT, confidence=LOW,
        reason=reason, basis=LIVE, window_start=window_start,
        window_end=departure, departure=departure, flight_count=len(everything),
        n_terminals=n_terminals, terminal_matched=False, note=note,
    )
