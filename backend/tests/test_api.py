from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import cache, db, history
from app.main import app


@pytest.fixture
def client(temp_db, monkeypatch):
    """A client whose app runs entirely on the mock provider."""
    monkeypatch.setattr("app.config.settings.provider_order", "mock")
    with TestClient(app) as c:
        yield c


def soon(days: int = 1) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")


# --- Health / introspection -------------------------------------------------

def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["live_data"] is False, "no keys set, so this must not claim live data"
    assert body["providers"][0]["provider"] == "mock"
    assert "min_samples_for_baseline" in body["corpus"]


def test_quota(client):
    body = client.get("/api/quota").json()
    assert "used_today" in body and "providers" in body


def test_airports_list(client):
    body = client.get("/api/airports").json()
    assert len(body) > 100
    assert {"iata", "icao", "name", "city", "country"} <= set(body[0])


def test_schedule_debug_endpoint(client):
    client.get("/api/predict/manual", params={"airport": "SFO", "flight_time": _t(6)})
    body = client.get("/api/schedule/SFO").json()
    assert body["flight_count"] > 0
    assert body["terminals"] >= 2
    assert body["data_source"] in ("cache", "fresh")


# --- Flight prediction ------------------------------------------------------

def test_predict_flight_happy_path(client):
    r = client.get("/api/predict/flight", params={"flight_no": "UA123", "date": soon()})
    assert r.status_code == 200
    body = r.json()

    assert body["flight"]["flight_no"] == "UA123"
    assert body["airport"]
    assert body["wait_category"] in ("Light", "Moderate", "Severe")
    assert body["confidence"] in ("high", "medium", "low")
    assert body["recommended_arrival_local"] < body["flight"]["departure_local"]
    assert body["assumptions"]["seats_per_flight"] == 150


def test_recommended_arrival_accounts_for_wait_and_buffer(client):
    body = client.get(
        "/api/predict/flight", params={"flight_no": "UA123", "date": soon()}
    ).json()

    departure = datetime.strptime(body["flight"]["departure_local"], "%Y-%m-%dT%H:%M")
    arrival = datetime.strptime(body["recommended_arrival_local"], "%Y-%m-%dT%H:%M")
    gap = (departure - arrival).total_seconds() / 60
    assert gap == body["estimated_wait_minutes"] + body["assumptions"]["gate_buffer_minutes"]


def test_repeat_request_uses_no_api_calls(client):
    params = {"flight_no": "UA123", "date": soon()}
    first = client.get("/api/predict/flight", params=params).json()
    second = client.get("/api/predict/flight", params=params).json()

    assert first["api_calls_used"] == 2, "novel flight + airport"
    assert second["api_calls_used"] == 0, "a repeat must be free"
    assert second["data_source"] == "cache"


def test_multiple_matches_returns_options(client):
    r = client.get("/api/predict/flight", params={"flight_no": "UA100", "date": soon()})
    assert r.status_code == 300

    body = r.json()
    assert body["code"] == "multiple_matches"
    assert len(body["options"]) == 2
    assert body["options"][0]["dep_iata"] != body["options"][1]["dep_iata"]
    assert body["options"][0]["dep_airport_name"]


def test_disambiguation_resolves_to_the_chosen_leg(client):
    date = soon()
    options = client.get(
        "/api/predict/flight", params={"flight_no": "UA100", "date": date}
    ).json()["options"]
    chosen = options[1]["dep_iata"]

    r = client.get(
        "/api/predict/flight",
        params={"flight_no": "UA100", "date": date, "dep_iata": chosen},
    )
    assert r.status_code == 200
    assert r.json()["airport"] == chosen


def test_invalid_flight_number(client):
    r = client.get("/api/predict/flight", params={"flight_no": "nonsense", "date": soon()})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_flight_number"


def test_unknown_flight(client):
    r = client.get("/api/predict/flight", params={"flight_no": "UA9001", "date": soon()})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "flight_not_found"


def test_bad_date_format(client):
    r = client.get("/api/predict/flight", params={"flight_no": "UA123", "date": "10-08-2026"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_far_future_flight_without_history_is_honest(client):
    """No board that far out and an empty corpus: say so, do not invent a number."""
    r = client.get(
        "/api/predict/flight", params={"flight_no": "UA123", "date": soon(days=30)}
    )
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "out_of_range"
    assert "history" in body["message"] or "not available" in body["message"]


def test_far_future_flight_uses_baseline_once_history_exists(client):
    """The corpus makes far-future answerable at zero API cost."""
    target = datetime.now() + timedelta(days=30)
    for week in range(4):
        day = target - timedelta(days=7 * (week + 1))
        flights = [
            _flight(day.replace(hour=h, minute=5), "2")
            for h in range(0, 24)
            for _ in range(6)
        ]
        history.record_board(
            "SFO", flights, day.replace(hour=0, minute=0), day.replace(hour=23, minute=0)
        )

    r = client.get(
        "/api/predict/manual",
        params={
            "airport": "SFO",
            "flight_time": target.strftime("%Y-%m-%dT%H:%M"),
            "terminal": "2",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["basis"] == "baseline"
    assert body["confidence"] == "low"
    assert body["api_calls_used"] == 0, "the baseline must cost nothing"
    assert "previous" in body["confidence_reason"]


# --- Manual prediction ------------------------------------------------------

def _t(_hours_ahead: int = 24) -> str:
    """A departure time at a reliably busy hour.

    Deliberately *not* `now + N hours`. That lands in the small hours for part
    of the day, where the diurnal curve is near-dead and a terminal filter
    legitimately matches zero flights — so the assertion's outcome depended on
    what time the suite happened to run. Tomorrow morning is always busy.
    """
    tomorrow = datetime.now() + timedelta(days=1)
    return tomorrow.replace(hour=9, minute=0, second=0, microsecond=0).strftime(
        "%Y-%m-%dT%H:%M"
    )


def _flight(when, terminal):
    from app.normalize import NormalizedFlight

    return NormalizedFlight(
        dep_iata="SFO", dep_time_local=when, flight_iata="UA1",
        dep_terminal=terminal, source_provider="mock",
    )


def test_manual_prediction(client):
    r = client.get(
        "/api/predict/manual",
        params={"airport": "SFO", "flight_time": _t(6), "terminal": "2"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["airport"] == "SFO"
    assert body["airport_name"] == "San Francisco International"
    assert body["scope"] == "terminal"
    assert body["terminal"] == "2"


def test_manual_normalizes_terminal_input(client):
    """'Terminal 2' and '2' must reach the same answer."""
    a = client.get(
        "/api/predict/manual",
        params={"airport": "SFO", "flight_time": _t(6), "terminal": "Terminal 2"},
    ).json()
    b = client.get(
        "/api/predict/manual",
        params={"airport": "SFO", "flight_time": _t(6), "terminal": "2"},
    ).json()
    assert a["terminal"] == b["terminal"] == "2"
    assert a["flights_in_window"] == b["flights_in_window"]


def test_manual_without_terminal_widens_to_airport(client):
    body = client.get(
        "/api/predict/manual", params={"airport": "SFO", "flight_time": _t(6)}
    ).json()
    assert body["scope"] == "airport"
    assert body["confidence"] == "low"
    assert body["capacity_per_hour"] > 750, "capacity must scale with scope"


def test_manual_rejects_typo_airport_without_spending_a_call(client):
    before = client.get("/api/quota").json()["used_today"]
    r = client.get(
        "/api/predict/manual", params={"airport": "SFX", "flight_time": _t(6)}
    )
    after = client.get("/api/quota").json()["used_today"]

    assert r.status_code == 400
    assert r.json()["error"]["code"] == "airport_unknown"
    assert after == before, "a typo reached a provider"


def test_manual_rejects_bad_time(client):
    r = client.get(
        "/api/predict/manual", params={"airport": "SFO", "flight_time": "tomorrow"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


# --- Degradation ------------------------------------------------------------

def test_budget_exhaustion_returns_its_own_code(client, monkeypatch):
    """Users need 'try tomorrow', not 'something broke'."""
    from tests.test_router import set_caps

    set_caps(monkeypatch, {"mock": 1})
    monkeypatch.setattr("app.config.settings.daily_allowance_min", 0)
    monkeypatch.setattr("app.config.settings.daily_allowance_max", 0)

    r = client.get(
        "/api/predict/manual", params={"airport": "LAX", "flight_time": _t(6)}
    )
    assert r.status_code in (422, 503)
    if r.status_code == 503:
        assert r.json()["error"]["code"] == "budget_exhausted"
        assert "cached" in r.json()["error"]["message"]


def test_stale_data_is_labelled(client):
    client.get("/api/predict/manual", params={"airport": "SFO", "flight_time": _t(6)})
    with db.connect() as conn:
        conn.execute("UPDATE airport_schedule_cache SET fetched_at = ?", (db.now_ts() - 99999,))

    body = client.get(
        "/api/schedule/SFO"
    ).json()
    assert body["data_source"] == "stale"
    assert body["cache_age_minutes"] > 60


def test_error_envelope_is_consistent(client):
    """Every failure has the same shape, so the UI renders one thing."""
    failures = [
        client.get("/api/predict/flight", params={"flight_no": "!!", "date": soon()}),
        client.get("/api/predict/flight", params={"flight_no": "UA9001", "date": soon()}),
        client.get("/api/predict/manual", params={"airport": "ZZZ", "flight_time": _t(6)}),
    ]
    for r in failures:
        assert r.status_code >= 400
        body = r.json()
        assert set(body) == {"error"}
        assert {"code", "message"} <= set(body["error"])
        assert isinstance(body["error"]["message"], str) and body["error"]["message"]


def test_openapi_schema_builds(client):
    """Catches response-model mismatches that would only show at runtime."""
    assert client.get("/openapi.json").status_code == 200
