from __future__ import annotations

import pytest

from app import airports, db

EXPECTED_TABLES = {
    "airport_schedule_cache",
    "flights",
    "flight_resolution_cache",
    "flight_terminal_history",
    "terminal_density_history",
    "api_usage",
    "provider_state",
}


def test_schema_creates_all_tables(temp_db):
    with db.connect() as conn:
        found = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert EXPECTED_TABLES <= found


def test_init_db_is_idempotent(temp_db):
    db.init_db()
    db.init_db()
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM flights").fetchone()["c"]
    assert count == 0


def test_wal_mode_enabled(temp_db):
    with db.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_rollback_on_error(temp_db):
    """A failed transaction must leave nothing behind."""
    with pytest.raises(RuntimeError):
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO flights (iata, dep_time_local, source_provider) VALUES (?,?,?)",
                ("SFO", "2026-08-10 14:30", "mock"),
            )
            raise RuntimeError("boom")

    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM flights").fetchone()["c"]
    assert count == 0


def test_day_and_month_keys():
    from datetime import datetime

    dt = datetime(2026, 8, 9, 17, 33)
    assert db.day_key(dt) == "2026-08-09"
    assert db.month_key(dt) == "2026-08"


# --- Airport reference ------------------------------------------------------

def test_known_airports_resolve():
    assert airports.is_known("SFO")
    assert airports.is_known("sfo")
    assert airports.lookup("SFO")["icao"] == "KSFO"


def test_manual_validation_rejects_typos():
    """The whole point: a typo must never reach a provider."""
    with pytest.raises(airports.UnknownAirportError):
        airports.validate_manual_iata("SFX")
    with pytest.raises(airports.UnknownAirportError):
        airports.validate_manual_iata("XX")
    with pytest.raises(airports.UnknownAirportError):
        airports.validate_manual_iata("")


def test_manual_validation_returns_canonical_code():
    assert airports.validate_manual_iata(" sfo ") == "SFO"


def test_well_formed_is_independent_of_known():
    """Resolution-derived codes rely on well-formedness only, not membership."""
    assert airports.is_well_formed("ZZZ")
    assert not airports.is_known("ZZZ")


def test_airport_list_is_non_trivial():
    every = airports.all_airports()
    assert len(every) > 100
    assert all(len(a["iata"]) == 3 for a in every)
