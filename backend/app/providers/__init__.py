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


def build_providers(order: list[str] | None = None) -> list[ScheduleProvider]:
    """Instantiate providers in priority order, skipping unconfigured ones."""
    names = order if order is not None else settings.provider_names
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
