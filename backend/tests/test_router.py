"""Router and budget behaviour.

The distinctions under test are the ones that cost real money if wrong:
quota exhaustion must be sticky, a transient blip must not be, and a
not-found must not fan out across every provider.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import budget, db, router
from app.config import settings
from app.providers.base import (
    FlightNotFound,
    ProviderAuthError,
    ProviderQuotaExceeded,
    ProviderResult,
    ProviderTransientError,
    ScheduleProvider,
)
from app.router import AllProvidersUnavailable, ProviderRouter


class FakeProvider(ScheduleProvider):
    """A provider whose next outcome the test dictates."""

    supports_flight_lookup = True
    supports_airport_departures = True

    def __init__(self, name, *, raises=None, calls_used=1, flights=None):
        self.name = name
        self.raises = raises
        self.calls_used = calls_used
        self.flights = flights or []
        self.call_count = 0

    async def fetch_departures(self, iata, window_start, window_end):
        self.call_count += 1
        if self.raises:
            raise self.raises
        return ProviderResult(
            provider=self.name, calls_used=self.calls_used, flights=self.flights
        )

    async def resolve_flight(self, flight_no, flight_date):
        self.call_count += 1
        if self.raises:
            raise self.raises
        return ProviderResult(provider=self.name, calls_used=self.calls_used)


def set_caps(monkeypatch, caps: dict[str, int]) -> None:
    """Patch the cap lookup on the class.

    pydantic's BaseSettings rejects setting an attribute that is not a declared
    field, so the method cannot be patched on the instance.
    """
    monkeypatch.setattr(
        type(settings), "monthly_cap_for", lambda self, p: caps.get(p, 0)
    )


@pytest.fixture
def metered(monkeypatch):
    """Give the fake providers real caps so budget logic actually engages."""
    set_caps(monkeypatch, {"primary": 100, "secondary": 100})
    return settings


WINDOW = (datetime(2026, 8, 10, 6, 0), datetime(2026, 8, 10, 18, 0))


# --- Failover ---------------------------------------------------------------

async def test_falls_over_to_secondary_on_quota(temp_db, metered):
    primary = FakeProvider("primary", raises=ProviderQuotaExceeded("out", "primary"))
    secondary = FakeProvider("secondary")
    result = await ProviderRouter([primary, secondary]).fetch_departures("SFO", *WINDOW)

    assert result.provider == "secondary"
    assert primary.call_count == 1 and secondary.call_count == 1


async def test_falls_over_on_transient(temp_db, metered):
    primary = FakeProvider("primary", raises=ProviderTransientError("timeout", "primary"))
    secondary = FakeProvider("secondary")
    result = await ProviderRouter([primary, secondary]).fetch_departures("SFO", *WINDOW)
    assert result.provider == "secondary"


async def test_all_failing_raises_with_reasons(temp_db, metered):
    providers = [
        FakeProvider("primary", raises=ProviderQuotaExceeded("out", "primary")),
        FakeProvider("secondary", raises=ProviderTransientError("boom", "secondary")),
    ]
    with pytest.raises(AllProvidersUnavailable) as exc:
        await ProviderRouter(providers).fetch_departures("SFO", *WINDOW)

    assert set(exc.value.reasons) == {"primary", "secondary"}
    assert "quota" in exc.value.reasons["primary"]


# --- The two states must not be confused ------------------------------------

async def test_quota_exhaustion_is_sticky(temp_db, metered):
    """A second request must not re-probe an exhausted provider."""
    primary = FakeProvider("primary", raises=ProviderQuotaExceeded("out", "primary"))
    secondary = FakeProvider("secondary")
    r = ProviderRouter([primary, secondary])

    await r.fetch_departures("SFO", *WINDOW)
    await r.fetch_departures("LAX", *WINDOW)

    assert primary.call_count == 1, "exhausted provider was re-probed"
    assert secondary.call_count == 2


async def test_exhaustion_clears_when_the_month_rolls_over(temp_db, metered):
    router.mark_exhausted("primary", "out")
    assert not router.get_status("primary").is_available

    # Rewrite the recorded month to simulate the rollover.
    with db.connect() as conn:
        conn.execute(
            "UPDATE provider_state SET month_key = ? WHERE provider = ?",
            ("2020-01", "primary"),
        )
    assert router.get_status("primary").is_available


async def test_transient_failure_is_not_sticky(temp_db, metered):
    """A timeout must not disqualify a provider for the month."""
    primary = FakeProvider("primary", raises=ProviderTransientError("blip", "primary"))
    secondary = FakeProvider("secondary")
    r = ProviderRouter([primary, secondary])

    await r.fetch_departures("SFO", *WINDOW)
    assert router.get_status("primary").state == router.DEGRADED

    # Expire the backoff; the provider becomes eligible again.
    with db.connect() as conn:
        conn.execute(
            "UPDATE provider_state SET state_until = ? WHERE provider = ?",
            (db.now_ts() - 1, "primary"),
        )
    primary.raises = None
    result = await r.fetch_departures("LAX", *WINDOW)

    assert result.provider == "primary", "recovered provider was not retried"
    assert router.get_status("primary").state == router.HEALTHY


async def test_backoff_grows_then_caps(temp_db, metered):
    for expected_failures in (1, 2, 3):
        router.mark_degraded("primary", "boom")
        assert router.get_status("primary").failure_count == expected_failures

    for _ in range(10):
        router.mark_degraded("primary", "boom")
    status = router.get_status("primary")
    assert status.state_until - db.now_ts() <= router._BACKOFF_MAX_SECONDS


async def test_success_clears_degraded_state(temp_db, metered):
    router.mark_degraded("primary", "blip")
    primary = FakeProvider("primary")
    with db.connect() as conn:
        conn.execute("UPDATE provider_state SET state_until = ?", (db.now_ts() - 1,))

    await ProviderRouter([primary]).fetch_departures("SFO", *WINDOW)
    assert router.get_status("primary").state == router.HEALTHY
    assert router.get_status("primary").failure_count == 0


# --- Not-found must not fan out ---------------------------------------------

async def test_flight_not_found_does_not_fail_over(temp_db, metered):
    """Every provider would give the same answer; trying them wastes budget."""
    primary = FakeProvider("primary", raises=FlightNotFound("nope", "primary"))
    secondary = FakeProvider("secondary")

    with pytest.raises(FlightNotFound):
        await ProviderRouter([primary, secondary]).resolve_flight("UA9999", "2026-08-10")

    assert secondary.call_count == 0, "not-found fanned out to another provider"


async def test_not_found_still_charges_budget(temp_db, metered):
    primary = FakeProvider("primary", raises=FlightNotFound("nope", "primary"))
    with pytest.raises(FlightNotFound):
        await ProviderRouter([primary]).resolve_flight("UA9999", "2026-08-10")
    assert budget.monthly_used("primary") == 1


# --- Auth ------------------------------------------------------------------

async def test_auth_error_is_not_charged(temp_db, metered):
    """A missing key means no call left the machine."""
    primary = FakeProvider("primary", raises=ProviderAuthError("no key", "primary"))
    secondary = FakeProvider("secondary")
    await ProviderRouter([primary, secondary]).fetch_departures("SFO", *WINDOW)
    assert budget.monthly_used("primary") == 0


# --- Capability -------------------------------------------------------------

async def test_unsupported_capability_is_skipped_without_a_call(temp_db, metered):
    primary = FakeProvider("primary")
    primary.supports_airport_departures = False
    secondary = FakeProvider("secondary")

    result = await ProviderRouter([primary, secondary]).fetch_departures("SFO", *WINDOW)
    assert result.provider == "secondary"
    assert primary.call_count == 0


# --- Budget accounting ------------------------------------------------------

async def test_pagination_cost_is_recorded_honestly(temp_db, metered):
    """Six pages is six calls, not one request."""
    primary = FakeProvider("primary", calls_used=6)
    await ProviderRouter([primary]).fetch_departures("SFO", *WINDOW)
    assert budget.monthly_used("primary") == 6


async def test_monthly_cap_blocks_before_dispatch(temp_db, metered, monkeypatch):
    set_caps(monkeypatch, {"primary": 3})
    primary = FakeProvider("primary", calls_used=3)
    r = ProviderRouter([primary])

    await r.fetch_departures("SFO", *WINDOW)          # spends the cap
    with pytest.raises(AllProvidersUnavailable):
        await r.fetch_departures("LAX", *WINDOW)

    assert primary.call_count == 1, "dispatched despite an exhausted cap"


async def test_blocked_calls_are_not_charged(temp_db, metered, monkeypatch):
    set_caps(monkeypatch, {"primary": 1})
    primary = FakeProvider("primary")
    r = ProviderRouter([primary])
    await r.fetch_departures("SFO", *WINDOW)
    with pytest.raises(AllProvidersUnavailable):
        await r.fetch_departures("LAX", *WINDOW)

    assert budget.monthly_used("primary") == 1
    with db.connect() as conn:
        blocked = conn.execute(
            "SELECT COUNT(*) c FROM api_usage WHERE status='blocked'"
        ).fetchone()["c"]
    assert blocked == 1


async def test_mock_provider_is_never_metered(temp_db):
    from app.providers import MockProvider

    r = ProviderRouter([MockProvider()])
    await r.fetch_departures("SFO", *WINDOW)
    assert budget.monthly_used("mock") == 0
    assert not budget.is_metered("mock")


# --- Daily allowance --------------------------------------------------------

def test_daily_allowance_is_self_balancing(temp_db, metered, monkeypatch):
    set_caps(monkeypatch, {"primary": 300})
    monkeypatch.setattr(settings, "daily_allowance_min", 10)
    monkeypatch.setattr(settings, "daily_allowance_max", 60)

    mid_month = datetime(2026, 8, 16, 12, 0)   # 16 days left, 300 remaining
    assert budget.daily_allowance(["primary"], mid_month) == 18

    late_month = datetime(2026, 8, 30, 12, 0)  # 2 days left, 300 remaining -> capped
    assert budget.daily_allowance(["primary"], late_month) == 60


def test_daily_allowance_never_reaches_zero(temp_db, metered, monkeypatch):
    """A burst throttles later days but must not empty the month."""
    set_caps(monkeypatch, {"primary": 300})
    budget.record("primary", "departures", "ok", calls=299)
    assert budget.daily_allowance(["primary"]) >= settings.daily_allowance_min


def test_days_left_in_month():
    assert budget.days_left_in_month(datetime(2026, 8, 31)) == 1
    assert budget.days_left_in_month(datetime(2026, 8, 1)) == 31
    assert budget.days_left_in_month(datetime(2026, 2, 28)) == 1  # non-leap


async def test_daily_allowance_blocks_then_cached_paths_still_work(temp_db, metered, monkeypatch):
    """Exceeding the daily allowance blocks new calls but is not permanent."""
    set_caps(monkeypatch, {"primary": 900})
    monkeypatch.setattr(settings, "daily_allowance_min", 5)
    monkeypatch.setattr(settings, "daily_allowance_max", 5)

    budget.record("primary", "departures", "ok", calls=5)
    decision = budget.check("primary", ["primary"])
    assert not decision.allowed
    assert "cached airports still work" in decision.reason


def test_snapshot_reports_pooled_and_daily(temp_db, metered):
    budget.record("primary", "departures", "ok", calls=4)
    snap = budget.snapshot(["primary", "secondary", "mock"])

    assert snap["used_today"] == 4
    assert snap["pooled_remaining"] == 196
    by_name = {p["provider"]: p for p in snap["providers"]}
    assert by_name["primary"]["used_this_month"] == 4
    assert by_name["mock"]["metered"] is False


def test_errors_count_toward_budget_but_blocked_do_not(temp_db, metered):
    budget.record("primary", "departures", "ok", calls=1)
    budget.record("primary", "departures", "error", calls=1)
    budget.record("primary", "departures", "blocked", calls=0)
    assert budget.monthly_used("primary") == 2
