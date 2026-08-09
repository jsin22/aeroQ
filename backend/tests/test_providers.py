from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.providers import MockProvider, build_providers
from app.providers.base import FlightNotFound, ProviderResult
from app.providers.mock import TERMINAL_HORIZON_HOURS


@pytest.fixture
def mock():
    return MockProvider()


# --- Determinism ------------------------------------------------------------
# The mock is the entire development path, so instability here would surface as
# flaky tests three phases downstream.

async def test_departures_are_deterministic(mock):
    start = datetime(2026, 8, 10, 6, 0)
    end = start + timedelta(hours=6)
    a = await mock.fetch_departures("SFO", start, end)
    b = await mock.fetch_departures("SFO", start, end)

    assert [(f.dep_time_local, f.flight_iata) for f in a.flights] == [
        (f.dep_time_local, f.flight_iata) for f in b.flights
    ]


def test_seed_is_process_stable():
    """hash() is salted per process; the seed must not be."""
    from app.providers.mock import _seed

    assert _seed("board", "SFO", "2026-08-10 06") == _seed("board", "SFO", "2026-08-10 06")
    assert _seed("board", "SFO", "2026-08-10 06") != _seed("board", "LAX", "2026-08-10 06")


async def test_resolution_is_deterministic(mock):
    a = await mock.resolve_flight("UA123", "2026-08-10")
    b = await mock.resolve_flight("ua 123", "2026-08-10")  # same flight, typed differently
    assert a.resolutions[0].dep_iata == b.resolutions[0].dep_iata
    assert a.resolutions[0].dep_time_local == b.resolutions[0].dep_time_local


# --- Realism ----------------------------------------------------------------

async def test_window_is_respected(mock):
    start = datetime(2026, 8, 10, 9, 0)
    end = datetime(2026, 8, 10, 12, 0)
    result = await mock.fetch_departures("SFO", start, end)
    assert result.flights
    assert all(start <= f.dep_time_local < end for f in result.flights)


async def test_flights_are_sorted(mock):
    start = datetime(2026, 8, 10, 6, 0)
    result = await mock.fetch_departures("SFO", start, start + timedelta(hours=8))
    times = [f.dep_time_local for f in result.flights]
    assert times == sorted(times)


async def test_diurnal_curve_has_a_dead_overnight(mock):
    """Density must vary by hour, or the prediction math is never exercised."""
    day = datetime(2026, 8, 10)
    night = await mock.fetch_departures("SFO", day.replace(hour=2), day.replace(hour=4))
    morning = await mock.fetch_departures("SFO", day.replace(hour=7), day.replace(hour=9))
    assert len(morning.flights) > len(night.flights) * 3


async def test_hub_is_busier_than_small_airport(mock):
    start = datetime(2026, 8, 10, 8, 0)
    end = start + timedelta(hours=2)
    hub = await mock.fetch_departures("SFO", start, end)
    small = await mock.fetch_departures("BOI", start, end)
    assert len(hub.flights) > len(small.flights)


async def test_terminals_are_populated_and_normalized(mock):
    start = datetime(2026, 8, 10, 8, 0)
    result = await mock.fetch_departures("SFO", start, start + timedelta(hours=4))
    terminals = {f.dep_terminal_norm for f in result.flights}
    assert len(terminals - {None}) >= 2, "a hub should span multiple terminals"
    assert None in terminals, "some flights must lack a terminal, as in reality"


async def test_small_airport_has_one_terminal(mock):
    start = datetime(2026, 8, 10, 8, 0)
    result = await mock.fetch_departures("BOI", start, start + timedelta(hours=6))
    assert {f.dep_terminal_norm for f in result.flights} - {None} == {"1"}


# --- Cost accounting --------------------------------------------------------

async def test_departures_cost_one_call_regardless_of_size(mock):
    """The property that makes a time-windowed endpoint worth preferring."""
    start = datetime(2026, 8, 10, 6, 0)
    big = await mock.fetch_departures("SFO", start, start + timedelta(hours=12))
    small = await mock.fetch_departures("BOI", start, start + timedelta(hours=12))
    assert big.calls_used == small.calls_used == 1
    assert len(big.flights) > len(small.flights)


# --- Resolution behaviours the downstream phases rely on --------------------

async def test_terminal_is_none_beyond_horizon(mock):
    """Mirrors reality: terminals are not assigned weeks ahead."""
    far = (datetime.now() + timedelta(hours=TERMINAL_HORIZON_HOURS + 240)).strftime("%Y-%m-%d")
    result = await mock.resolve_flight("UA123", far)
    assert all(r.dep_terminal_norm is None for r in result.resolutions)


async def test_multi_leg_flight_returns_several_options(mock):
    """Numbers divisible by 100 are the multi-leg fixture."""
    result = await mock.resolve_flight("UA100", "2026-08-10")
    assert len(result.resolutions) == 2
    assert result.resolutions[0].dep_iata != result.resolutions[1].dep_iata


async def test_single_leg_flight_returns_one(mock):
    result = await mock.resolve_flight("UA123", "2026-08-10")
    assert len(result.resolutions) == 1


async def test_unknown_flight_raises(mock):
    with pytest.raises(FlightNotFound):
        await mock.resolve_flight("UA9001", "2026-08-10")


async def test_malformed_flight_number_raises(mock):
    with pytest.raises(FlightNotFound):
        await mock.resolve_flight("not-a-flight", "2026-08-10")


async def test_airline_resolves_to_plausible_hub(mock):
    """A United flight should not depart from Heathrow."""
    result = await mock.resolve_flight("UA123", "2026-08-10")
    assert result.resolutions[0].dep_iata in {"SFO", "ORD", "EWR", "IAH", "DEN", "LAX"}


# --- Registry ---------------------------------------------------------------

def test_build_providers_skips_unconfigured():
    """No API keys is a supported mode, not an error."""
    built = build_providers(["aerodatabox", "airlabs", "mock"])
    assert [p.name for p in built] == ["mock"]


def test_build_providers_falls_back_to_mock_when_empty():
    built = build_providers(["aerodatabox"])
    assert [p.name for p in built] == ["mock"]


def test_build_providers_ignores_unknown_names():
    built = build_providers(["nonsense", "mock"])
    assert [p.name for p in built] == ["mock"]


def test_mock_is_dropped_once_a_real_provider_is_configured(monkeypatch):
    """Invented schedules must never stand in for real ones.

    Leaving mock at the end of the chain would mean an exhausted or
    unreachable real provider silently falls through to fabricated flights
    presented as a genuine prediction.
    """
    monkeypatch.setattr("app.config.settings.airlabs_api_key", "test-key")
    built = build_providers(["airlabs", "mock"])
    assert [p.name for p in built] == ["airlabs"]


def test_mock_fallback_can_be_opted_into(monkeypatch):
    monkeypatch.setattr("app.config.settings.airlabs_api_key", "test-key")
    built = build_providers(["airlabs", "mock"], allow_mock_fallback=True)
    assert [p.name for p in built] == ["airlabs", "mock"]


def test_mock_survives_when_it_is_the_only_provider():
    """Zero-config development mode stays supported."""
    assert [p.name for p in build_providers(["aerodatabox", "mock"])] == ["mock"]


def test_capability_flags_are_declared():
    for provider in build_providers(["mock"]):
        assert isinstance(provider.supports_flight_lookup, bool)
        assert isinstance(provider.supports_airport_departures, bool)


def test_provider_result_defaults():
    r = ProviderResult(provider="x", calls_used=1)
    assert r.flights == [] and r.resolutions == [] and r.partial is False
