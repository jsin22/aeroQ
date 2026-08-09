"""Request and response schemas.

Every prediction response carries three things beyond the number itself:
`confidence` with a human-readable reason, `data_source` with the cache age,
and the `assumptions` block. The estimate is a heuristic, not a model fitted to
observed waits, so the UI has to be able to show its work — a bare "Severe"
with no provenance would imply an authority this does not have.
"""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

ISO_MINUTE = "%Y-%m-%dT%H:%M"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def iso(dt: datetime | None) -> str | None:
    return dt.strftime(ISO_MINUTE) if dt else None


# --- Errors -----------------------------------------------------------------

class ErrorBody(BaseModel):
    code: str
    message: str
    detail: dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# --- Shared ----------------------------------------------------------------

class Assumptions(BaseModel):
    seats_per_flight: int
    origin_passenger_factor: float
    lanes_per_terminal: int
    passengers_per_lane_per_hour: int
    rush_window_hours: float
    gate_buffer_minutes: int


class RushWindow(BaseModel):
    start: str
    end: str


class FlightInfo(BaseModel):
    flight_no: str
    date: str
    departure_local: str | None = None
    arrival_airport: str | None = None


class PredictionResponse(BaseModel):
    flight: FlightInfo | None = None
    airport: str
    airport_name: str | None = None
    terminal: str | None = None
    scope: str = Field(description="'terminal' or 'airport'")
    confidence: str = Field(description="'high', 'medium' or 'low'")
    confidence_reason: str
    basis: str = Field(description="'live' schedule or historical 'baseline'")
    terminal_matched: bool

    rush_window: RushWindow
    flights_in_window: float
    estimated_passengers: int
    demand_per_hour: int
    capacity_per_hour: int
    load_ratio: float
    wait_category: str
    estimated_wait_minutes: int
    recommended_arrival_local: str

    data_source: str = Field(description="'fresh', 'cache', 'stale' or 'none'")
    cache_age_minutes: int | None = None
    source_provider: str | None = None
    api_calls_used: int = 0
    note: str | None = None
    assumptions: Assumptions


class FlightOption(BaseModel):
    """One leg of an ambiguous flight number, for the user to choose between."""

    dep_iata: str
    dep_airport_name: str | None = None
    dep_terminal: str | None = None
    departure_local: str | None = None
    arr_iata: str | None = None


class MultipleMatches(BaseModel):
    code: str = "multiple_matches"
    message: str
    options: list[FlightOption]


# --- Health / quota ---------------------------------------------------------

class ProviderHealth(BaseModel):
    provider: str
    state: str
    available: bool
    reason: str | None = None
    last_error: str | None = None


class HealthResponse(BaseModel):
    status: str
    version: str
    live_data: bool = Field(
        description="False when only the mock provider is active"
    )
    providers: list[ProviderHealth]
    corpus: dict


class ProviderQuota(BaseModel):
    provider: str
    monthly_cap: int
    used_this_month: int
    remaining: int | None = None
    metered: bool
    reported_remaining: int | None = None
    reported_counter: str | None = None


class QuotaResponse(BaseModel):
    month: str
    day: str
    days_left_in_month: int
    providers: list[ProviderQuota]
    pooled_remaining: int | None = None
    used_today: int
    daily_allowance: int | None = None


class AirportInfo(BaseModel):
    iata: str
    icao: str
    name: str
    city: str
    country: str


# --- Request validation -----------------------------------------------------
# Validation happens here so malformed input is rejected before it can reach a
# provider and spend budget.

class FlightQuery(BaseModel):
    flight_no: str
    date: str
    dep_iata: str | None = None

    @field_validator("date")
    @classmethod
    def _check_date(cls, v: str) -> str:
        if not _DATE_RE.match(v):
            raise ValueError("date must be YYYY-MM-DD")
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("date is not a real calendar date") from exc
        return v


class ManualQuery(BaseModel):
    airport: str
    flight_time: str
    terminal: str | None = None

    @field_validator("flight_time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        cleaned = v.strip().replace(" ", "T")
        for fmt in (ISO_MINUTE, "%Y-%m-%dT%H:%M:%S"):
            try:
                datetime.strptime(cleaned, fmt)
                return cleaned
            except ValueError:
                continue
        raise ValueError("flight_time must look like 2026-08-10T14:30")
