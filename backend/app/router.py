"""Provider routing: which source to ask, and when to stop asking.

The central decision is that routing is **pre-emptive**, not reactive. The
router consults the local ledger before dispatching and picks the first
provider with budget left. Failing over on a 429 instead would mean the call
was already spent before we learned anything — one wasted call per request for
the remainder of the month — and would make a transient 500 indistinguishable
from quota exhaustion. The 429 handling below is a backstop that corrects local
drift, not the primary signal.

The state machine keeps the two apart:

    exhausted  quota gone. Sticky until the month rolls over; retrying costs a
               call and cannot succeed.
    degraded   timeout / 5xx. Exponential backoff, then retried. Explicitly not
               disqualifying for the month — the provider may answer next time.
    healthy    eligible.

State is persisted rather than held in memory: a crash loop that forgot which
providers were exhausted would re-probe them on every restart and burn the
pooled budget.

`FlightNotFound` is deliberately *not* a failover trigger. It means the call
succeeded and the flight genuinely does not exist, so every other provider will
also fail to find it — trying them would spend budget to confirm the same
answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from . import budget, db
from .providers.base import (
    FlightNotFound,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderResult,
    ProviderTransientError,
    ProviderUnsupported,
    ScheduleProvider,
)

log = logging.getLogger(__name__)

HEALTHY = "healthy"
DEGRADED = "degraded"
EXHAUSTED = "exhausted"

# Backoff for transient failures: 30s, 60s, 120s, ... capped at 5 minutes.
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_MAX_SECONDS = 300

# An auth failure needs operator action, so retrying soon is pointless noise.
_AUTH_BACKOFF_SECONDS = 3600


class AllProvidersUnavailable(Exception):
    """No provider could serve the request.

    `reasons` records why each was skipped or failed, so the API can explain
    the outcome instead of returning a bare failure.
    """

    def __init__(self, reasons: dict[str, str]) -> None:
        self.reasons = reasons
        detail = "; ".join(f"{k}: {v}" for k, v in reasons.items()) or "no providers"
        super().__init__(f"all providers unavailable ({detail})")


@dataclass
class ProviderStatus:
    provider: str
    state: str
    state_until: int | None
    month_key: str | None
    last_error: str | None
    failure_count: int

    @property
    def is_available(self) -> bool:
        now = db.now_ts()
        if self.state == EXHAUSTED:
            # Sticky only for the month it was recorded in.
            return self.month_key != db.month_key()
        if self.state == DEGRADED:
            return self.state_until is None or self.state_until <= now
        return True

    def unavailable_reason(self) -> str:
        if self.state == EXHAUSTED:
            return f"quota exhausted for {self.month_key}"
        if self.state == DEGRADED:
            wait = max(0, (self.state_until or 0) - db.now_ts())
            return f"backing off after {self.failure_count} failure(s), {wait}s left"
        return "available"


def get_status(provider: str) -> ProviderStatus:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM provider_state WHERE provider = ?", (provider,)
        ).fetchone()
    if row is None:
        return ProviderStatus(provider, HEALTHY, None, None, None, 0)
    return ProviderStatus(
        provider=row["provider"],
        state=row["state"],
        state_until=row["state_until"],
        month_key=row["month_key"],
        last_error=row["last_error"],
        failure_count=row["failure_count"] or 0,
    )


def _set_status(
    provider: str,
    state: str,
    *,
    state_until: int | None = None,
    month: str | None = None,
    error: str | None = None,
    failure_count: int = 0,
) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO provider_state
                (provider, state, state_until, month_key, last_error, failure_count, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(provider) DO UPDATE SET
                state = excluded.state,
                state_until = excluded.state_until,
                month_key = excluded.month_key,
                last_error = excluded.last_error,
                failure_count = excluded.failure_count,
                updated_at = excluded.updated_at
            """,
            (provider, state, state_until, month, error, failure_count, db.now_ts()),
        )


def mark_healthy(provider: str) -> None:
    _set_status(provider, HEALTHY)


def mark_exhausted(provider: str, error: str) -> None:
    """Sticky for the current month — see module docstring."""
    _set_status(provider, EXHAUSTED, month=db.month_key(), error=error)


def mark_degraded(provider: str, error: str, *, auth: bool = False) -> None:
    previous = get_status(provider)
    failures = (previous.failure_count if previous.state == DEGRADED else 0) + 1
    if auth:
        backoff = _AUTH_BACKOFF_SECONDS
    else:
        backoff = min(_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)), _BACKOFF_MAX_SECONDS)
    _set_status(
        provider,
        DEGRADED,
        state_until=db.now_ts() + backoff,
        error=error,
        failure_count=failures,
    )


class ProviderRouter:
    def __init__(self, providers: list[ScheduleProvider]) -> None:
        self.providers = providers

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.providers]

    async def resolve_flight(self, flight_no: str, flight_date: str) -> ProviderResult:
        return await self._dispatch(
            capability="supports_flight_lookup",
            endpoint="resolve",
            call=lambda p: p.resolve_flight(flight_no, flight_date),
        )

    async def fetch_departures(
        self, iata: str, window_start: datetime, window_end: datetime
    ) -> ProviderResult:
        return await self._dispatch(
            capability="supports_airport_departures",
            endpoint="departures",
            call=lambda p: p.fetch_departures(iata, window_start, window_end),
        )

    async def _dispatch(self, capability: str, endpoint: str, call) -> ProviderResult:
        reasons: dict[str, str] = {}

        for provider in self.providers:
            name = provider.name

            if not getattr(provider, capability):
                reasons[name] = f"does not support {endpoint}"
                continue

            status = get_status(name)
            if not status.is_available:
                reasons[name] = status.unavailable_reason()
                continue

            decision = budget.check(name, self.names)
            if not decision.allowed:
                reasons[name] = decision.reason or "out of budget"
                budget.record(name, endpoint, "blocked", calls=0, detail=decision.reason)
                continue

            try:
                result = await call(provider)

            except FlightNotFound:
                # A successful call that found nothing. Budget was spent, and
                # failing over cannot help — no provider will find a flight
                # that does not exist.
                budget.record(name, endpoint, "ok", calls=1, detail="not found")
                mark_healthy(name)
                raise

            except ProviderQuotaExceeded as exc:
                budget.record(name, endpoint, "error", calls=1, detail=str(exc))
                mark_exhausted(name, str(exc))
                reasons[name] = f"quota exceeded: {exc}"
                log.warning("provider %s exhausted: %s", name, exc)
                continue

            except ProviderAuthError as exc:
                # No call reached the provider if the key is simply missing, so
                # this is not charged against the budget.
                budget.record(name, endpoint, "blocked", calls=0, detail=str(exc))
                mark_degraded(name, str(exc), auth=True)
                reasons[name] = f"auth error: {exc}"
                log.error("provider %s auth failure: %s", name, exc)
                continue

            except ProviderUnsupported as exc:
                reasons[name] = str(exc)
                continue

            except ProviderTransientError as exc:
                budget.record(name, endpoint, "error", calls=1, detail=str(exc))
                mark_degraded(name, str(exc))
                reasons[name] = f"transient failure: {exc}"
                log.warning("provider %s degraded: %s", name, exc)
                continue

            except ProviderError as exc:  # pragma: no cover - defensive
                budget.record(name, endpoint, "error", calls=1, detail=str(exc))
                mark_degraded(name, str(exc))
                reasons[name] = f"error: {exc}"
                continue

            budget.record(
                name,
                endpoint,
                "ok",
                calls=result.calls_used,
                detail="partial" if result.partial else None,
            )
            mark_healthy(name)
            return result

        raise AllProvidersUnavailable(reasons)

    async def aclose(self) -> None:
        for provider in self.providers:
            await provider.aclose()

    def status_snapshot(self) -> list[dict]:
        out = []
        for provider in self.providers:
            status = get_status(provider.name)
            out.append(
                {
                    "provider": provider.name,
                    "state": status.state,
                    "available": status.is_available,
                    "reason": None if status.is_available else status.unavailable_reason(),
                    "last_error": status.last_error,
                }
            )
        return out
