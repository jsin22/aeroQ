"""Provider registry.

Unconfigured providers are dropped at build time rather than failing at call
time, so running with no API keys at all is a supported mode (everything falls
through to MockProvider) instead of an error.
"""

from __future__ import annotations

import logging

from ..config import settings
from .aerodatabox import AeroDataBoxProvider
from .airlabs import AirLabsProvider
from .base import (
    FlightNotFound,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaExceeded,
    ProviderResult,
    ProviderTransientError,
    ProviderUnsupported,
    ScheduleProvider,
)
from .mock import MockProvider

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[ScheduleProvider]] = {
    "aerodatabox": AeroDataBoxProvider,
    "airlabs": AirLabsProvider,
    "mock": MockProvider,
}


def build_providers(
    order: list[str] | None = None,
    *,
    allow_mock_fallback: bool | None = None,
) -> list[ScheduleProvider]:
    """Instantiate providers in priority order, skipping unconfigured ones.

    **Mock is removed once any real provider is configured.** It invents
    plausible schedules, so leaving it at the end of the chain would mean an
    exhausted or unreachable real provider silently falls through to fabricated
    flights presented as a genuine prediction. Failing honestly is the only
    acceptable behaviour there — `budget_exhausted` tells the user to try
    later, whereas invented data tells them to leave for the airport at the
    wrong time.

    With no real provider configured, mock is the whole app, which is the
    supported zero-config development mode.
    """
    names = order if order is not None else settings.provider_names
    if allow_mock_fallback is None:
        allow_mock_fallback = settings.allow_mock_fallback

    built: list[ScheduleProvider] = []
    for name in names:
        cls = _REGISTRY.get(name)
        if cls is None:
            log.warning("unknown provider %r in PROVIDER_ORDER — ignoring", name)
            continue
        provider = cls()
        if not provider.is_configured:
            log.info("provider %r has no credentials — skipping", name)
            continue
        built.append(provider)

    real = [p for p in built if p.name != "mock"]
    if real and not allow_mock_fallback:
        dropped = len(built) - len(real)
        if dropped:
            log.info(
                "live providers configured (%s) — removing mock from the chain so "
                "invented data can never stand in for real schedules",
                ", ".join(p.name for p in real),
            )
        built = real

    if not built:
        log.warning("no providers configured; falling back to mock")
        built = [MockProvider()]

    return built


__all__ = [
    "AeroDataBoxProvider",
    "AirLabsProvider",
    "MockProvider",
    "ScheduleProvider",
    "ProviderResult",
    "ProviderError",
    "ProviderQuotaExceeded",
    "ProviderTransientError",
    "ProviderAuthError",
    "ProviderUnsupported",
    "FlightNotFound",
    "build_providers",
]
