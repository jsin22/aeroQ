"""Resolution and board caching — where the cost model is enforced.

The cost table from BUILD_PLAN.md §5 is asserted directly at the bottom:
0 calls when both steps hit, 2 only for a genuinely novel flight and airport.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from app import budget, cache, db, resolver
from app.config import settings
from app.providers import MockProvider
from app.providers.base import ProviderResult, ProviderTransientError, ScheduleProvider
from app.resolver import InvalidFlightNumber, compute_recheck_after
from app.router import ProviderRouter


@pytest.fixture
def router():
    return ProviderRouter([MockProvider()])


class CountingProvider(ScheduleProvider):
    """Wraps the mock so tests can count real fetches."""

    name = "mock"
    supports_flight_lookup = True
    supports_airport_departures = True

    def __init__(self, delay: float = 0.0):
        self.inner = MockProvider()
        self.fetches = 0
        self.resolves = 0
        self.delay = delay

    async def fetch_departures(self, iata, window_start, window_end):
        self.fetches += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return await self.inner.fetch_departures(iata, window_start, window_end)

    async def resolve_flight(self, flight_no, flight_date):
        self.resolves += 1
        return await self.inner.resolve_flight(flight_no, flight_date)


# --- Dynamic TTL ------------------------------------------------------------
# The rule that makes the terminal dead zone free.

def test_recheck_is_scheduled_for_when_a_terminal_can_first_exist():
    now = datetime(2026, 8, 1, 12, 0)
    departure = now + timedelta(days=21)
    got = datetime.fromtimestamp(compute_recheck_after(departure, None, now))

    assert got == departure - timedelta(hours=resolver.TERMINAL_AVAILABLE_HOURS)
    assert got - now > timedelta(days=18), "would have re-polled inside the dead zone"


def test_no_terminal_inside_the_window_polls_gently():
    now = datetime(2026, 8, 1, 12, 0)
    departure = now + timedelta(hours=30)  # inside 72h, still unassigned
    got = datetime.fromtimestamp(compute_recheck_after(departure, None, now))
    assert got == now + timedelta(seconds=resolver.NO_TERMINAL_RECHECK_SECONDS)


def test_known_terminal_rechecks_before_departure():
    now = datetime(2026, 8, 1, 12, 0)
    departure = now + timedelta(days=2)
    got = datetime.fromtimestamp(compute_recheck_after(departure, "2", now))
    assert got == departure - timedelta(hours=6)


def test_recheck_never_schedules_in_the_past():
    """Close-in departures must not trigger a refetch on every request."""
    now = datetime(2026, 8, 1, 12, 0)
    departure = now + timedelta(hours=1)
    got = datetime.fromtimestamp(compute_recheck_after(departure, "2", now))
    assert got >= now + timedelta(seconds=resolver.MIN_RECHECK_SECONDS)


def test_departed_flights_are_cached_long():
    now = datetime(2026, 8, 1, 12, 0)
    got = datetime.fromtimestamp(compute_recheck_after(now - timedelta(hours=3), "2", now))
    assert got >= now + timedelta(hours=23)


# --- Resolution -------------------------------------------------------------

async def test_malformed_flight_number_costs_nothing(temp_db):
    counting = CountingProvider()
    with pytest.raises(InvalidFlightNumber):
        await resolver.resolve(ProviderRouter([counting]), "not-a-flight", "2026-08-10")
    assert counting.resolves == 0


async def test_second_lookup_of_same_flight_is_free(temp_db):
    """The halving that makes step 1 worth caching at all."""
    counting = CountingProvider()
    r = ProviderRouter([counting])
    date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    first = await resolver.resolve(r, "UA123", date)
    second = await resolver.resolve(r, "UA123", date)

    assert first.calls_used == 1 and first.from_cache is False
    assert second.calls_used == 0 and second.from_cache is True
    assert counting.resolves == 1


async def test_same_flight_typed_differently_hits_one_cache_entry(temp_db):
    counting = CountingProvider()
    r = ProviderRouter([counting])
    date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    await resolver.resolve(r, "UA123", date)
    await resolver.resolve(r, "ua 123", date)
    assert counting.resolves == 1


async def test_multi_leg_is_returned_not_collapsed(temp_db, router):
    date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    outcome = await resolver.resolve(router, "UA100", date)
    assert outcome.ambiguous
    assert len(outcome.resolutions) == 2


async def test_disambiguation_is_free(temp_db):
    """Picking a leg must not cost a second call."""
    counting = CountingProvider()
    r = ProviderRouter([counting])
    date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    first = await resolver.resolve(r, "UA100", date)
    chosen = first.resolutions[1].dep_iata
    narrowed = await resolver.resolve(r, "UA100", date, prefer_iata=chosen)

    assert counting.resolves == 1
    assert narrowed.calls_used == 0
    assert len(narrowed.resolutions) == 1
    assert narrowed.single.dep_iata == chosen


async def test_far_future_flight_is_resolved_without_a_terminal(temp_db, router):
    far = (datetime.now() + timedelta(days=21)).strftime("%Y-%m-%d")
    outcome = await resolver.resolve(router, "UA123", far)
    assert outcome.single.dep_iata
    assert outcome.single.dep_terminal_norm is None


# --- Board cache ------------------------------------------------------------

async def test_board_is_fetched_then_served_from_cache(temp_db):
    counting = CountingProvider()
    r = ProviderRouter([counting])
    departure = datetime.now() + timedelta(hours=6)

    first = await cache.get_board(r, "SFO", departure)
    second = await cache.get_board(r, "SFO", departure)

    assert first.data_source == cache.FRESH and first.calls_used == 1
    assert second.data_source == cache.CACHE and second.calls_used == 0
    assert counting.fetches == 1


async def test_stale_board_triggers_a_refetch(temp_db):
    counting = CountingProvider()
    r = ProviderRouter([counting])
    departure = datetime.now() + timedelta(hours=6)

    await cache.get_board(r, "SFO", departure)
    with db.connect() as conn:
        conn.execute(
            "UPDATE airport_schedule_cache SET fetched_at = ?",
            (db.now_ts() - int(settings.cache_ttl_hours * 3600) - 60,),
        )

    again = await cache.get_board(r, "SFO", departure)
    assert again.data_source == cache.FRESH
    assert counting.fetches == 2


async def test_fresh_board_that_misses_the_window_is_refetched(temp_db):
    """Fresh is not enough — the board has to actually cover the question."""
    counting = CountingProvider()
    r = ProviderRouter([counting])

    await cache.get_board(r, "SFO", datetime.now() + timedelta(hours=3))
    far = await cache.get_board(r, "SFO", datetime.now() + timedelta(days=3))

    assert counting.fetches == 2
    assert far.data_source == cache.FRESH


async def test_concurrent_requests_cost_one_call(temp_db):
    """Five friends, one airport, one second: one call."""
    counting = CountingProvider(delay=0.05)
    r = ProviderRouter([counting])
    departure = datetime.now() + timedelta(hours=6)

    results = await asyncio.gather(
        *(cache.get_board(r, "SFO", departure) for _ in range(5))
    )

    assert counting.fetches == 1, "the re-check after acquiring the lock did not hold"
    assert all(res.flights for res in results)


async def test_concurrent_requests_to_different_airports_are_not_serialised(temp_db):
    counting = CountingProvider(delay=0.05)
    r = ProviderRouter([counting])
    departure = datetime.now() + timedelta(hours=6)

    await asyncio.gather(
        cache.get_board(r, "SFO", departure),
        cache.get_board(r, "LAX", departure),
    )
    assert counting.fetches == 2


async def test_beyond_horizon_spends_nothing(temp_db):
    """No provider has a board weeks out; paying to find that out is waste."""
    counting = CountingProvider()
    r = ProviderRouter([counting])

    result = await cache.get_board(r, "SFO", datetime.now() + timedelta(days=30))
    assert counting.fetches == 0
    assert result.data_source == cache.NONE
    assert "days out" in result.note


async def test_provider_failure_serves_stale_data(temp_db):
    counting = CountingProvider()
    r = ProviderRouter([counting])
    departure = datetime.now() + timedelta(hours=6)
    await cache.get_board(r, "SFO", departure)

    class Broken(ScheduleProvider):
        name = "broken"
        supports_airport_departures = True

        async def fetch_departures(self, *a):
            raise ProviderTransientError("down", "broken")

    with db.connect() as conn:
        conn.execute(
            "UPDATE airport_schedule_cache SET fetched_at = ?",
            (db.now_ts() - int(settings.cache_ttl_hours * 3600) - 60,),
        )

    degraded = await cache.get_board(ProviderRouter([Broken()]), "SFO", departure)
    assert degraded.data_source == cache.STALE
    assert degraded.flights, "stale data was discarded instead of served"
    assert "unavailable" in degraded.note


async def test_no_board_and_no_provider_returns_none_source(temp_db):
    class Broken(ScheduleProvider):
        name = "broken"
        supports_airport_departures = True

        async def fetch_departures(self, *a):
            raise ProviderTransientError("down", "broken")

    result = await cache.get_board(
        ProviderRouter([Broken()]), "SFO", datetime.now() + timedelta(hours=6)
    )
    assert result.data_source == cache.NONE
    assert result.flights == []


async def test_board_is_replaced_not_appended(temp_db):
    """The table must never hold a mix of two fetches."""
    counting = CountingProvider()
    r = ProviderRouter([counting])
    departure = datetime.now() + timedelta(hours=6)

    await cache.get_board(r, "SFO", departure)
    with db.connect() as conn:
        first_count = conn.execute(
            "SELECT COUNT(*) c FROM flights WHERE iata='SFO'"
        ).fetchone()["c"]
        conn.execute("UPDATE airport_schedule_cache SET fetched_at = 0")

    await cache.get_board(r, "SFO", departure)
    with db.connect() as conn:
        second_count = conn.execute(
            "SELECT COUNT(*) c FROM flights WHERE iata='SFO'"
        ).fetchone()["c"]

    assert first_count == second_count


async def test_count_terminals_reflects_the_board(temp_db, router):
    await cache.get_board(router, "SFO", datetime.now() + timedelta(hours=6))
    assert cache.count_terminals("SFO") >= 2
    assert cache.count_terminals("BOI") == 0  # never fetched


def test_plan_window_centres_on_departure():
    departure = datetime(2026, 8, 10, 14, 30)
    start, end = cache.plan_window(departure)
    assert start < departure < end
    assert (end - start) == timedelta(hours=settings.departure_block_hours)


# --- The cost table from BUILD_PLAN.md §5 -----------------------------------

async def test_cost_table(temp_db, monkeypatch):
    """Novel flight+airport = 2 calls; everything after = 0."""
    from tests.test_router import set_caps

    set_caps(monkeypatch, {"mock": 500})  # meter the mock so the ledger counts
    counting = CountingProvider()
    r = ProviderRouter([counting])
    date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    outcome = await resolver.resolve(r, "UA123", date)
    departure = outcome.single.dep_time_local
    await cache.get_board(r, outcome.single.dep_iata, departure)
    assert budget.monthly_used("mock") == 2, "novel flight + airport should cost 2"

    # A second person on the same flight pays nothing.
    outcome2 = await resolver.resolve(r, "UA123", date)
    await cache.get_board(r, outcome2.single.dep_iata, departure)
    assert budget.monthly_used("mock") == 2, "a repeat query should be free"
    assert outcome2.calls_used == 0
