"""Shared fixtures.

Every test runs against a throwaway database in a temp directory. `settings` is
a cached singleton, so the DB path is patched on the live object and the
`get_settings` cache cleared, which keeps tests from ever touching a real
aeroq.db.
"""

from __future__ import annotations

import pytest

from app import db
from app.config import get_settings, settings


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
    monkeypatch.setattr(settings, "rush_window_hours", 2.0)
    monkeypatch.setattr(settings, "lanes_per_terminal", 5)
    monkeypatch.setattr(settings, "pax_per_lane_per_hour", 150)
    monkeypatch.setattr(settings, "light_max_ratio", 0.6)
    monkeypatch.setattr(settings, "moderate_max_ratio", 1.0)
    monkeypatch.setattr(settings, "base_wait_min", 5.0)
    monkeypatch.setattr(settings, "saturated_wait_min", 25.0)
    monkeypatch.setattr(settings, "max_wait_min", 120.0)
    monkeypatch.setattr(settings, "gate_buffer_min", 45)
    return settings
