"""Provider interface and the error vocabulary the router reasons about.

Two design points carry most of the weight here:

1. **`ProviderResult.calls_used`.** A provider reports how much budget it
   actually spent. This is not bookkeeping pedantry — it is the whole reason
   the abstraction exists. A time-windowed endpoint returns a complete airport
   picture for one call; a count-paginated one spends a call per page, so the
   same logical request costs 1 at AeroDataBox and 6 at AirLabs for a large
   hub. Hiding that behind a uniform interface would make the budget ledger
   quietly wrong.

2. **Quota exhaustion and transient failure are different exceptions.**
   Collapsing them into "the call failed" is the bug that makes a provider get
   marked dead for a month because of one timeout, or makes the router retry a
   provider that has nothing left to give.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..normalize import FlightResolution, NormalizedFlight


class ProviderError(Exception):
    """Base for every provider failure."""

    def __init__(self, message: str, provider: str = "unknown") -> None:
        self.provider = provider
        super().__init__(message)


class ProviderQuotaExceeded(ProviderError):
    """The provider has no budget left.

    Sticky until the month rolls over: retrying costs a call and cannot
    succeed.
    """


class ProviderTransientError(ProviderError):
    """A timeout, connection failure, or 5xx.

    Explicitly *not* quota exhaustion — the provider may well answer the next
    request, so this must not disqualify it for the month.
    """


class ProviderAuthError(ProviderError):
    """Missing or rejected credentials. Not retryable without operator action."""


class FlightNotFound(ProviderError):
    """The provider answered successfully; the flight simply does not exist.

    A successful call that found nothing. It still spent budget, and it must
    not trigger failover — every other provider will also fail to find it.
    """


class ProviderUnsupported(ProviderError):
    """The provider does not implement this capability."""


@dataclass
class ProviderResult:
    """What a provider returns, including what it cost."""

    provider: str
    calls_used: int
    flights: list[NormalizedFlight] = field(default_factory=list)
    resolutions: list[FlightResolution] = field(default_factory=list)
    raw: Any = None
    partial: bool = False
    partial_reason: str | None = None


class ScheduleProvider(ABC):
    """One aviation data source.

    Capability flags let the router skip a provider it cannot use for a given
    call, rather than spending a request to discover that.
    """

    name: str = "base"
    supports_flight_lookup: bool = False
    supports_airport_departures: bool = False

    @property
    def is_configured(self) -> bool:
        """Whether this provider has what it needs to be called at all.

        Unconfigured providers are dropped from the routing order at startup,
        so a missing API key is a non-event rather than a runtime failure.
        """
        return True

    async def resolve_flight(self, flight_no: str, flight_date: str) -> ProviderResult:
        """Step 1: flight number + date -> departure airport, terminal, time.

        Returns every matching leg. One flight number can cover multiple legs
        on the same date, and picking the first silently would strand users on
        the wrong one.
        """
        raise ProviderUnsupported(
            f"{self.name} does not support flight lookup", self.name
        )

    async def fetch_departures(
        self, iata: str, window_start: datetime, window_end: datetime
    ) -> ProviderResult:
        """Step 2: every departure from an airport within a local-time window."""
        raise ProviderUnsupported(
            f"{self.name} does not support airport departures", self.name
        )

    async def aclose(self) -> None:
        """Release any held network resources."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r} configured={self.is_configured}>"
