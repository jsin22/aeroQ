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
    """n flights spread across a 2-hour span from `start`."""
    return [
        make_flight(start + timedelta(minutes=int(120 * i / max(n, 1))), terminal)
        for i in range(n)
    ]


def at_departure(n: int, terminal: str | None) -> list[NormalizedFlight]:
    """n flights leaving exactly with ours — each carries full co-queue weight."""
    return [make_flight(DEPARTURE, terminal) for _ in range(n)]


# --- The arithmetic ---------------------------------------------------------

def test_worked_example(temp_db, frozen_settings):
    """18 flights leaving alongside ours: full weight, so 18 effective."""
    board = make_board(at_departure(18, "2"))
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
    board = make_board(at_departure(10, "2"))
    p = predict.predict(board, DEPARTURE, terminal_norm="2")

    assert p.estimated_passengers == 1125          # the raw window total
    assert p.demand_per_hour == 562                # halved before comparison
    assert p.load_ratio == pytest.approx(0.75, abs=0.01)


# --- Co-queuing: flights after yours count too -------------------------------

def test_flight_leaving_with_yours_counts_fully(frozen_settings):
    assert predict.co_queue_weight(DEPARTURE, DEPARTURE) == 1.0


def test_flights_before_and_after_count_equally(frozen_settings):
    """The correction: people arriving early for later flights are in your queue."""
    before = predict.co_queue_weight(DEPARTURE - timedelta(hours=1), DEPARTURE)
    after = predict.co_queue_weight(DEPARTURE + timedelta(hours=1), DEPARTURE)
    assert before == after == pytest.approx(0.5)


def test_weight_decays_to_zero_at_the_window_edge(frozen_settings):
    assert predict.co_queue_weight(DEPARTURE + timedelta(hours=2), DEPARTURE) == 0.0
    assert predict.co_queue_weight(DEPARTURE - timedelta(hours=2), DEPARTURE) == 0.0
    assert predict.co_queue_weight(DEPARTURE + timedelta(hours=5), DEPARTURE) == 0.0


def test_later_flights_contribute_to_the_estimate(temp_db, frozen_settings):
    """A board with departures only *after* ours must not read as empty."""
    later = [
        make_flight(DEPARTURE + timedelta(minutes=30), "2") for _ in range(12)
    ]
    board = make_board(later, window=(
        DEPARTURE - timedelta(hours=6), DEPARTURE + timedelta(hours=6)
    ))
    p = predict.predict(board, DEPARTURE, terminal_norm="2")

    assert p.flights_in_window > 0, "flights after yours were ignored"
    assert p.flights_in_window == pytest.approx(12 * 0.75, abs=0.1)


def test_security_window_is_reported_not_the_counted_span(temp_db, frozen_settings):
    """The window shown is when you are at security, not what was counted."""
    board = make_board(at_departure(6, "2"))
    p = predict.predict(board, DEPARTURE, terminal_norm="2")

    assert p.rush_window_start == DEPARTURE - timedelta(minutes=165)
    assert p.rush_window_end == DEPARTURE - timedelta(minutes=45)


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
    """Flights far outside the co-queuing span contribute nothing."""
    board = make_board(spread(3, "2", start=datetime(2026, 8, 10, 5, 0)))
    p = predict.predict(board, DEPARTURE, terminal_norm="2")
    assert p.flights_in_window == 0
    assert p.wait_category == predict.LIGHT
    assert p.estimated_wait_minutes == 5


def test_contribution_falls_off_linearly(temp_db, frozen_settings):
    """One flight at each offset: 1.0 + 0.5 + 0.5 + 0.0 = 2.0 effective."""
    board = make_board(
        [
            make_flight(DEPARTURE, "2"),                            # 1.0
            make_flight(DEPARTURE - timedelta(hours=1), "2"),       # 0.5
            make_flight(DEPARTURE + timedelta(hours=1), "2"),       # 0.5
            make_flight(DEPARTURE + timedelta(hours=3), "2"),       # 0.0
        ],
        window=(DEPARTURE - timedelta(hours=6), DEPARTURE + timedelta(hours=6)),
    )
    p = predict.predict(board, DEPARTURE, terminal_norm="2")
    assert p.flights_in_window == pytest.approx(2.0, abs=0.01)


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
        flights += at_departure(12, terminal)
    board = make_board(flights)
    cache.store_board("SFO", flights, board.window_start, board.window_end, "mock", {})

    p = predict.predict(board, DEPARTURE, terminal_norm=None)

    assert p.scope == predict.AIRPORT
    assert p.capacity_per_hour == 3000, "capacity did not scale with scope"
    assert p.wait_category != predict.SEVERE, "the v1 capacity bug is back"
    assert p.flights_in_window == 48


def test_same_density_gives_same_category_at_either_scope(temp_db, frozen_settings):
    """Scope should not change the answer when load per terminal is identical."""
    per_terminal = at_departure(12, "1")
    single = make_board(per_terminal)
    terminal_pred = predict.predict(single, DEPARTURE, terminal_norm="1")

    flights = []
    for terminal in ["1", "2", "3", "4"]:
        flights += at_departure(12, terminal)
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
    board = make_board(at_departure(10, "2") + at_departure(6, "3"))
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
    board = make_board(at_departure(20, "1"))
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
    assert p.flights_in_window > 0
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


# --- Lane estimation --------------------------------------------------------
# Lane counts are not published anywhere usable, so they are inferred from the
# airport's busiest observed hour. These tests use real settings (no
# frozen_settings), since that fixture disables estimation.

def test_lanes_scale_with_airport_size(temp_db):
    """A big hub must be sized larger than a regional field."""
    hub = [make_flight(DEPARTURE.replace(hour=8, minute=m % 60), str(1 + m % 4))
           for m in range(40)]
    cache.store_board("SFO", hub, DEPARTURE.replace(hour=0),
                      DEPARTURE.replace(hour=23), "mock", {})

    small = [make_flight(DEPARTURE.replace(hour=8, minute=m * 10), "1")
             for m in range(3)]
    cache.store_board("BOI", small, DEPARTURE.replace(hour=0),
                      DEPARTURE.replace(hour=23), "mock", {})

    hub_lanes, hub_source = predict.estimate_lanes("SFO", predict.AIRPORT, None, 4)
    small_lanes, _ = predict.estimate_lanes("BOI", predict.AIRPORT, None, 1)

    assert hub_lanes > small_lanes
    assert "estimated" in hub_source


def test_lane_estimate_is_clamped(temp_db):
    """A degenerate board must not produce an absurd checkpoint."""
    absurd = [make_flight(DEPARTURE.replace(hour=8), "1") for _ in range(2000)]
    cache.store_board("SFO", absurd, DEPARTURE.replace(hour=0),
                      DEPARTURE.replace(hour=23), "mock", {})
    lanes, _ = predict.estimate_lanes("SFO", predict.AIRPORT, None, 1)
    assert lanes <= settings.max_estimated_lanes


def test_lane_estimate_falls_back_without_a_board(temp_db):
    lanes, source = predict.estimate_lanes("ZZZ", predict.AIRPORT, None, 1)
    assert lanes == settings.lanes_per_terminal
    assert "no schedule" in source


def test_peak_hour_sizing_keeps_the_signal(temp_db):
    """The trap this design avoids.

    Sizing capacity from *current* demand would make every hour read the same.
    Sizing from the peak and measuring hour by hour must leave a quiet hour
    reading lighter than the peak hour.
    """
    flights = []
    for _ in range(40):                                   # 08:00 peak
        flights.append(make_flight(DEPARTURE.replace(hour=8), "1"))
    for _ in range(6):                                    # 14:30 lull
        flights.append(make_flight(DEPARTURE, "1"))
    cache.store_board("SFO", flights, DEPARTURE.replace(hour=0),
                      DEPARTURE.replace(hour=23), "mock", {})
    board = make_board(flights, window=(DEPARTURE.replace(hour=0),
                                        DEPARTURE.replace(hour=23)))

    quiet = predict.predict(board, DEPARTURE, terminal_norm="1")
    busy = predict.predict(board, DEPARTURE.replace(hour=8), terminal_norm="1")

    assert quiet.load_ratio < busy.load_ratio, "capacity tracked demand; signal lost"
    assert quiet.capacity_per_hour == busy.capacity_per_hour, "capacity must be fixed"


def test_true_peak_can_read_severe(temp_db):
    """lane_design_factor < 1 exists so the busiest hour is not merely Moderate.

    Density has to be sustained across the co-queuing span, not spiked into one
    instant. That is not a workaround — it is the model being self-consistent.
    With a steady λ flights/hour the triangular weights integrate to 2λ over the
    ±2h span, so demand_per_hour comes back to λ and meets a capacity sized from
    that same λ, giving a ratio of exactly 1 / lane_design_factor. An isolated
    spike surrounded by empty hours genuinely *is* a lighter queue, because the
    people around you arrive over a spread of time, not all at once.
    """
    flights = [
        make_flight(DEPARTURE.replace(hour=h, minute=(i * 3) % 60), "1")
        for h in range(6, 13)
        for i in range(20)
    ]
    cache.store_board("SFO", flights, DEPARTURE.replace(hour=0),
                      DEPARTURE.replace(hour=23), "mock", {})
    board = make_board(flights, window=(DEPARTURE.replace(hour=0),
                                        DEPARTURE.replace(hour=23)))

    p = predict.predict(board, DEPARTURE.replace(hour=9), terminal_norm="1")

    assert p.wait_category == predict.SEVERE
    assert p.load_ratio == pytest.approx(1 / settings.lane_design_factor, abs=0.15)


def test_corpus_peak_preferred_over_board(temp_db):
    """A 12h board can miss the daily peak, which would undersize capacity."""
    board_flights = [make_flight(DEPARTURE, "1") for _ in range(5)]
    cache.store_board("SFO", board_flights, DEPARTURE.replace(hour=0),
                      DEPARTURE.replace(hour=23), "mock", {})
    from_board, _ = predict.estimate_lanes("SFO", predict.AIRPORT, None, 1)

    for week in range(4):
        day = DEPARTURE - timedelta(days=7 * (week + 1))
        busy = [make_flight(day.replace(hour=8), "1") for _ in range(30)]
        history.record_board("SFO", busy, day.replace(hour=0, minute=0),
                             day.replace(hour=23, minute=0))

    from_corpus, source = predict.estimate_lanes("SFO", predict.AIRPORT, None, 1)
    assert from_corpus > from_board
    assert "history" in source


def test_assumptions_report_the_lane_source(temp_db):
    flights = [make_flight(DEPARTURE, "1") for _ in range(10)]
    cache.store_board("SFO", flights, DEPARTURE.replace(hour=0),
                      DEPARTURE.replace(hour=23), "mock", {})
    board = make_board(flights, window=(DEPARTURE.replace(hour=0),
                                        DEPARTURE.replace(hour=23)))
    p = predict.predict(board, DEPARTURE, terminal_norm="1")

    assert p.assumptions["lanes"] > 0
    assert "estimated" in p.assumptions["lanes_source"]
