"""Prediction math and the fallback ladder.

The load-bearing test here is `test_airport_fallback_is_not_automatically_severe`:
in v1, widening demand to the whole airport while capacity stayed at one
terminal's 750/hour made every fallback report Severe.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import cache, db, history, predict
from app.cache import BoardResult
from app.config import settings
from app.normalize import NormalizedFlight
from app.predict import PredictionUnavailable

DEPARTURE = datetime(2026, 8, 10, 14, 30)
WINDOW_START = datetime(2026, 8, 10, 12, 30)


def make_flight(when: datetime, terminal: str | None, flight_iata="UA1") -> NormalizedFlight:
    return NormalizedFlight(
        dep_iata="SFO",
        dep_time_local=when,
        flight_iata=flight_iata,
        dep_terminal=terminal,
        source_provider="mock",
    )


def make_board(flights, *, window=(datetime(2026, 8, 10, 8, 0), datetime(2026, 8, 10, 20, 0))):
    return BoardResult(
        iata="SFO",
        flights=flights,
        data_source=cache.CACHE,
        cache_age_minutes=10,
        source_provider="mock",
        window_start=window[0],
        window_end=window[1],
    )


def spread(n: int, terminal: str | None, start=WINDOW_START) -> list[NormalizedFlight]:
    """n flights spread across the 2-hour rush window."""
    return [
        make_flight(start + timedelta(minutes=int(120 * i / max(n, 1))), terminal)
        for i in range(n)
    ]


# --- The arithmetic ---------------------------------------------------------

def test_worked_example_from_the_plan(temp_db, frozen_settings):
    """BUILD_PLAN.md §6: 18 flights -> 2025 pax -> ratio 1.35 -> Severe."""
    board = make_board(spread(18, "2"))
    p = predict.predict(board, DEPARTURE, terminal_norm="2")

    assert p.flights_in_window == 18
    assert p.estimated_passengers == 2025
    assert p.demand_per_hour == 1012
    assert p.capacity_per_hour == 750
    assert p.load_ratio == 1.35
    assert p.wait_category == predict.SEVERE
    assert p.estimated_wait_minutes == 67
    assert p.recommended_arrival_local == DEPARTURE - timedelta(minutes=67 + 45)


def test_demand_is_a_rate_not_a_total(temp_db, frozen_settings):
    """The spec's 2h-total vs 1h-capacity comparison would double every result."""
    board = make_board(spread(10, "2"))
    p = predict.predict(board, DEPARTURE, terminal_norm="2")

    assert p.estimated_passengers == 1125          # the raw 2-hour total
    assert p.demand_per_hour == 562                # halved before comparison

    assert p.load_ratio == pytest.approx(0.75, abs=0.01)


@pytest.mark.parametrize(
    "ratio,expected",
    [(0.0, predict.LIGHT), (0.59, predict.LIGHT), (0.6, predict.MODERATE),
     (1.0, predict.MODERATE), (1.01, predict.SEVERE), (3.0, predict.SEVERE)],
)
def test_category_boundaries(frozen_settings, ratio, expected):
    assert predict.categorize(ratio) == expected


def test_wait_curve_is_monotonic_and_capped(frozen_settings):
    waits = [predict.estimate_wait_minutes(p, 750) for p in range(0, 8000, 250)]
    assert waits == sorted(waits)
    assert waits[0] == 5                        # idle checkpoint
    assert max(waits) <= settings.max_wait_min  # clamped


def test_wait_at_saturation(frozen_settings):
    """Exactly at capacity: 750/hr over 2h = 1500 passengers."""
    assert predict.estimate_wait_minutes(1500, 750) == 25


def test_empty_window_is_light(temp_db, frozen_settings):
    board = make_board(spread(0, "2") + spread(3, "2", start=datetime(2026, 8, 10, 9, 0)))
    p = predict.predict(board, DEPARTURE, terminal_norm="2")
    assert p.flights_in_window == 0
    assert p.wait_category == predict.LIGHT
    assert p.estimated_wait_minutes == 5


def test_window_is_exactly_two_hours_before_departure(temp_db, frozen_settings):
    inside = make_flight(datetime(2026, 8, 10, 12, 31), "2")
    too_early = make_flight(datetime(2026, 8, 10, 12, 29), "2")
    at_departure = make_flight(DEPARTURE, "2")       # end is exclusive
    at_start = make_flight(WINDOW_START, "2")        # start is inclusive

    board = make_board([inside, too_early, at_departure, at_start])
    p = predict.predict(board, DEPARTURE, terminal_norm="2")
    assert p.flights_in_window == 2


# --- Capacity scaling: the v1 bug -------------------------------------------

def test_capacity_scales_with_scope(frozen_settings):
    assert predict.capacity_for(predict.TERMINAL, 5) == 750
    assert predict.capacity_for(predict.AIRPORT, 5) == 3750
    assert predict.capacity_for(predict.AIRPORT, 0) == 750   # never divide by zero


def test_airport_fallback_is_not_automatically_severe(temp_db, frozen_settings):
    """The v1 bug: airport-wide demand against one terminal's capacity.

    A normal day across four terminals must not read Severe merely because the
    terminal was unknown.
    """
    flights = []
    for terminal in ["1", "2", "3", "4"]:
        flights += spread(12, terminal)
    board = make_board(flights)
    cache.store_board("SFO", flights, board.window_start, board.window_end, "mock", {})

    p = predict.predict(board, DEPARTURE, terminal_norm=None)

    assert p.scope == predict.AIRPORT
    assert p.capacity_per_hour == 3000, "capacity did not scale with scope"
    assert p.wait_category != predict.SEVERE, "the v1 capacity bug is back"
    assert p.flights_in_window == 48


def test_same_density_gives_same_category_at_either_scope(temp_db, frozen_settings):
    """Scope should not change the answer when load per terminal is identical."""
    per_terminal = spread(12, "1")
    single = make_board(per_terminal)
    terminal_pred = predict.predict(single, DEPARTURE, terminal_norm="1")

    flights = []
    for terminal in ["1", "2", "3", "4"]:
        flights += spread(12, terminal)
    board = make_board(flights)
    cache.store_board("SFO", flights, board.window_start, board.window_end, "mock", {})
    airport_pred = predict.predict(board, DEPARTURE, terminal_norm=None)

    assert terminal_pred.load_ratio == pytest.approx(airport_pred.load_ratio, abs=0.02)
    assert terminal_pred.wait_category == airport_pred.wait_category


# --- The fallback ladder ----------------------------------------------------

def test_level_1_terminal_reported(temp_db, frozen_settings):
    board = make_board(spread(10, "2"))
    p = predict.predict(board, DEPARTURE, terminal_norm="2")
    assert p.confidence == predict.HIGH
    assert p.scope == predict.TERMINAL
    assert p.terminal_matched is True
    assert p.basis == predict.LIVE


def test_level_2_terminal_inferred_from_history(temp_db, frozen_settings):
    board = make_board(spread(10, "2") + spread(6, "3"))
    cache.store_board("SFO", board.flights, board.window_start, board.window_end, "mock", {})
    history.record_terminals([make_flight(DEPARTURE, "2", flight_iata="UA123")] * 14)

    p = predict.predict(board, DEPARTURE, terminal_norm=None, flight_iata="UA123")

    assert p.confidence == predict.MEDIUM
    assert p.scope == predict.TERMINAL
    assert p.terminal == "2"
    assert p.flights_in_window == 10
    assert "usually departs Terminal 2" in p.confidence_reason
    assert p.terminal_matched is False, "an inferred terminal is not a reported one"


def test_level_3_no_terminal_anywhere(temp_db, frozen_settings):
    board = make_board(spread(10, "1") + spread(10, "2"))
    cache.store_board("SFO", board.flights, board.window_start, board.window_end, "mock", {})

    p = predict.predict(board, DEPARTURE, terminal_norm=None, flight_iata="XX999")

    assert p.confidence == predict.LOW
    assert p.scope == predict.AIRPORT
    assert "whole airport" in p.confidence_reason


def test_terminal_that_matches_nothing_widens_rather_than_reporting_zero(
    temp_db, frozen_settings
):
    """A board that disagrees with the resolution must not yield a false 'Light'."""
    board = make_board(spread(20, "1"))
    cache.store_board("SFO", board.flights, board.window_start, board.window_end, "mock", {})

    p = predict.predict(board, DEPARTURE, terminal_norm="7")

    assert p.scope == predict.AIRPORT
    assert p.flights_in_window == 20
    assert "No departures found for Terminal 7" in p.confidence_reason


def test_level_4_baseline_when_no_board(temp_db, frozen_settings):
    """Beyond board coverage, answer from accumulated history.

    9 flights in each of hours 12, 13 and 14. The window [12:30, 14:30) spans
    half of hour 12, all of 13 and half of 14, so the weighted total is 18.
    """
    for week in range(4):
        day = DEPARTURE - timedelta(days=7 * (week + 1))
        flights = [
            make_flight(day.replace(hour=h, minute=5), "2")
            for h in (12, 13, 14)
            for _ in range(9)
        ]
        history.record_board(
            "SFO", flights, day.replace(hour=0, minute=0), day.replace(hour=23, minute=0)
        )

    empty = BoardResult(iata="SFO", flights=[], data_source=cache.NONE)
    p = predict.predict(empty, DEPARTURE, terminal_norm="2")

    assert p.basis == predict.BASELINE
    assert p.confidence == predict.LOW
    assert p.flights_in_window == pytest.approx(18, abs=0.1)
    assert "previous Mondays" in p.confidence_reason


def test_baseline_weights_partial_hours(temp_db, frozen_settings):
    """A 2-hour window must not be summed from three whole hours."""
    for week in range(4):
        day = DEPARTURE - timedelta(days=7 * (week + 1))
        flights = [
            make_flight(day.replace(hour=h, minute=5), "2")
            for h in (12, 13, 14)
            for _ in range(10)
        ]
        history.record_board(
            "SFO", flights, day.replace(hour=0, minute=0), day.replace(hour=23, minute=0)
        )

    est = history.baseline_for_window("SFO", "2", WINDOW_START, DEPARTURE)
    assert est.flights == pytest.approx(20, abs=0.01), "unweighted sum would give 30"


def test_baseline_records_observed_zeros(temp_db):
    """An empty 3am must be distinguishable from an unobserved 3am."""
    day = datetime(2026, 8, 10)
    flights = [make_flight(day.replace(hour=12, minute=5), "2") for _ in range(4)]
    history.record_board("SFO", flights, day.replace(hour=0), day.replace(hour=6))

    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT avg_flights FROM terminal_density_history
            WHERE iata='SFO' AND terminal_norm='*' AND day_of_week=0 AND hour=3
            """
        ).fetchone()
    assert row is not None and row["avg_flights"] == 0.0


def test_level_5_raises_when_nothing_is_known(temp_db, frozen_settings):
    empty = BoardResult(iata="SFO", flights=[], data_source=cache.NONE)
    with pytest.raises(PredictionUnavailable) as exc:
        predict.predict(empty, DEPARTURE, terminal_norm="2")
    assert exc.value.code == "out_of_range"


def test_baseline_falls_back_from_terminal_to_airport(temp_db, frozen_settings):
    """Terminal history missing but airport history present."""
    for week in range(4):
        day = DEPARTURE - timedelta(days=7 * (week + 1))
        flights = [
            make_flight(day.replace(hour=h, minute=5), "1")
            for h in (12, 13, 14)
            for _ in range(10)
        ]
        history.record_board(
            "SFO", flights, day.replace(hour=0, minute=0), day.replace(hour=23, minute=0)
        )

    empty = BoardResult(iata="SFO", flights=[], data_source=cache.NONE)
    p = predict.predict(empty, DEPARTURE, terminal_norm="9")  # never observed

    assert p.basis == predict.BASELINE
    assert p.scope == predict.AIRPORT


def test_board_that_does_not_cover_the_window_is_not_used(temp_db, frozen_settings):
    """A board for the morning must not answer a question about the afternoon."""
    board = make_board(
        spread(10, "2"),
        window=(datetime(2026, 8, 10, 4, 0), datetime(2026, 8, 10, 10, 0)),
    )
    with pytest.raises(PredictionUnavailable):
        predict.predict(board, DEPARTURE, terminal_norm="2")


# --- Corpus -----------------------------------------------------------------

def test_corpus_ignores_repeat_observations_of_the_same_day(temp_db):
    day = datetime(2026, 8, 10)
    flights = [make_flight(day.replace(hour=12, minute=5), "2") for _ in range(6)]
    window = (day.replace(hour=0), day.replace(hour=23, minute=59))

    for _ in range(5):  # the same board refetched through the day
        history.record_board("SFO", flights, *window)

    stats = history.corpus_stats()
    assert stats["max_samples_per_slot"] == 1, "a refetch was counted as a new day"


def test_corpus_averages_across_distinct_days(temp_db):
    for week, count in enumerate([4, 8]):
        day = datetime(2026, 8, 10) - timedelta(days=7 * week)
        flights = [day.replace(hour=12, minute=5) for _ in range(count)]
        history.record_board(
            "SFO",
            [make_flight(f, "2") for f in flights],
            day.replace(hour=0),
            day.replace(hour=23, minute=59),
        )

    est = history.baseline_for_window(
        "SFO", "2", datetime(2026, 8, 10, 12, 0), datetime(2026, 8, 10, 13, 0)
    )
    assert est is None  # only 2 samples, below min_samples_for_baseline


def test_baseline_requires_minimum_samples(temp_db, monkeypatch):
    monkeypatch.setattr(settings, "min_samples_for_baseline", 3)
    day = datetime(2026, 8, 10)
    for week in range(2):
        d = day - timedelta(days=7 * week)
        history.record_board(
            "SFO",
            [make_flight(d.replace(hour=12, minute=5), "2")],
            d.replace(hour=0),
            d.replace(hour=23, minute=59),
        )
    assert history.baseline_for_window(
        "SFO", "2", day.replace(hour=12), day.replace(hour=13)
    ) is None


def test_modal_terminal_reports_its_support(temp_db):
    history.record_terminals([make_flight(DEPARTURE, "2", "UA123")] * 14)
    history.record_terminals([make_flight(DEPARTURE, "3", "UA123")])

    guess = history.modal_terminal("UA123")
    assert guess.terminal == "2"
    assert guess.observed == 14 and guess.total == 15
    assert "14 of 15" in guess.confidence_text


def test_modal_terminal_unknown_flight(temp_db):
    assert history.modal_terminal("ZZ999") is None
    assert history.modal_terminal(None) is None


def test_partial_hours_are_not_recorded(temp_db):
    """A half-covered hour would look quieter than it was, so it is skipped.

    Whole hours inside the window are still recorded, including explicit zeros
    — only the partially covered boundary hour is excluded.
    """
    day = datetime(2026, 8, 10)
    flights = [make_flight(day.replace(hour=12, minute=m), "2") for m in (5, 35)]
    # Window starts at 12:30, so hour 12 is only half covered.
    history.record_board("SFO", flights, day.replace(hour=12, minute=30), day.replace(hour=20))

    with db.connect() as conn:
        hours = {
            r["hour"]
            for r in conn.execute(
                "SELECT DISTINCT hour FROM terminal_density_history WHERE iata='SFO'"
            )
        }
    assert 12 not in hours, "a partially covered hour was recorded"
    assert 13 in hours and 19 in hours, "whole hours in the window should be recorded"


async def test_corpus_grows_from_normal_fetches(temp_db):
    """It must accumulate as a side effect, with no extra API cost."""
    from app.providers import MockProvider
    from app.router import ProviderRouter

    r = ProviderRouter([MockProvider()])
    await cache.get_board(r, "SFO", datetime.now() + timedelta(hours=6))

    stats = history.corpus_stats()
    assert stats["density_slots"] > 0
    assert stats["flights_with_terminal_history"] > 0
