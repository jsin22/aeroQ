"""Shared fixtures.

Every test runs against a throwaway database in a temp directory. `settings` is
a cached singleton, so the DB path is patched on the live object and the
`get_settings` cache cleared, which keeps tests from ever touching a real
aeroq.db.
"""

from __future__ import annotations

import asyncio

import pytest

from app import cache, db
from app.config import get_settings, settings


@pytest.fixture(autouse=True)
def no_live_providers(monkeypatch):
    """Blank every provider credential for the whole suite.

    `settings` reads the real .env, so once a live key exists on the machine
    the tests would build real providers and spend actual quota on every run.
    Tests that want a live provider must set a key explicitly.
    """
    monkeypatch.setattr(settings, "aerodatabox_api_key", "")
    monkeypatch.setattr(settings, "airlabs_api_key", "")
    monkeypatch.setattr(settings, "provider_order", "mock")
    monkeypatch.setattr(settings, "allow_mock_fallback", False)


@pytest.fixture(autouse=True)
def reset_airport_locks():
    """Drop the per-airport lock registry between tests.

    Production runs one event loop for the process lifetime, so the registry is
    correct there. pytest-asyncio creates a fresh loop per test, and an
    asyncio.Lock reused across loops raises once contended — a flake that would
    only ever appear in the concurrency tests, which are the ones worth
    trusting.
    """
    cache._locks.clear()
    cache._locks_guard = asyncio.Lock()
    yield
    cache._locks.clear()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    get_settings.cache_clear()
    db.init_db()
    yield db_file
    get_settings.cache_clear()


@pytest.fixture
def frozen_settings(monkeypatch):
    """Pin the prediction tunables so tests assert against known arithmetic."""
    monkeypatch.setattr(settings, "seats_per_flight", 150)
    monkeypatch.setattr(settings, "origin_pax_factor", 0.75)
    monkeypatch.setattr(settings, "security_lead_min_minutes", 45)
    monkeypatch.setattr(settings, "security_lead_max_minutes", 165)
    # Fixed lanes for the pure-arithmetic tests; estimation has its own tests.
    monkeypatch.setattr(settings, "estimate_lanes", False)
    monkeypatch.setattr(settings, "lanes_per_terminal", 5)
    monkeypatch.setattr(settings, "pax_per_lane_per_hour", 150)
    monkeypatch.setattr(settings, "light_max_ratio", 0.6)
    monkeypatch.setattr(settings, "moderate_max_ratio", 1.0)
    monkeypatch.setattr(settings, "base_wait_min", 5.0)
    monkeypatch.setattr(settings, "saturated_wait_min", 25.0)
    monkeypatch.setattr(settings, "max_wait_min", 120.0)
    monkeypatch.setattr(settings, "gate_buffer_min", 45)
    return settings
