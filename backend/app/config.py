"""Central configuration.

Every tunable in the prediction model lives here rather than inline, so the
heuristic can be retuned against observed reality without touching logic.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Providers ---------------------------------------------------------
    provider_order: str = "aerodatabox,airlabs,mock"

    aerodatabox_api_key: str = ""
    aerodatabox_api_host: str = "aerodatabox.p.rapidapi.com"
    # Minimum seconds between AeroDataBox requests. Its per-second rate limit
    # rejects back-to-back calls, and resolve-then-fetch is the normal path.
    aerodatabox_min_request_interval: float = 1.2
    # Stop this far short of the provider-reported budget, so the last calls of
    # the month are ours to spend rather than lost to an off-by-one.
    provider_quota_reserve: int = 10
    airlabs_api_key: str = ""

    # Mock is dropped automatically once a real provider is configured, so that
    # invented schedules can never stand in for real ones. Set true only to
    # deliberately test the mock path alongside live keys.
    allow_mock_fallback: bool = False

    # --- Budget ------------------------------------------------------------
    # Deliberately below the real free tiers so we stop before the provider does.
    aerodatabox_monthly_cap: int = 250
    airlabs_monthly_cap: int = 900
    daily_allowance_min: int = 10
    daily_allowance_max: int = 60

    # --- Cache -------------------------------------------------------------
    cache_ttl_hours: float = 4.0
    departure_block_hours: int = 12
    # Beyond this, no provider has a departure board to return, so spending a
    # call to discover that is pure waste. Requests past the horizon go
    # straight to the historical baseline.
    board_horizon_days: int = 7

    # --- Prediction --------------------------------------------------------
    seats_per_flight: int = 150
    origin_pax_factor: float = 0.75

    # When passengers are physically at security, relative to their departure.
    # Their crowd overlaps ours in proportion to how close the two departures
    # are, which is what makes flights leaving *after* yours count.
    security_lead_min_minutes: int = 45     # cleared by this point
    security_lead_max_minutes: int = 165    # arrived by this point

    rush_window_hours: float = 2.0          # retained; derived value below wins
    lanes_per_terminal: int = 5             # fallback when estimation is off
    pax_per_lane_per_hour: int = 150

    # Lane counts are not published anywhere usable, so they are inferred from
    # the airport's busiest observed hour - a proxy for what it was built to
    # handle. Sizing on the peak while measuring demand hour by hour is what
    # keeps the ratio meaningful; sizing on current demand would make every
    # airport read the same.
    estimate_lanes: bool = True
    lane_design_factor: float = 0.85        # <1 so a true peak can read Severe
    min_estimated_lanes: int = 2
    max_estimated_lanes: int = 80
    light_max_ratio: float = 0.6
    moderate_max_ratio: float = 1.0
    base_wait_min: float = 5.0
    saturated_wait_min: float = 25.0
    max_wait_min: float = 120.0
    gate_buffer_min: int = 45

    # --- History -----------------------------------------------------------
    min_samples_for_baseline: int = 3

    # --- Runtime -----------------------------------------------------------
    db_path: str = "./data/aeroq.db"
    log_level: str = "INFO"
    cors_origins: str = ""

    # --- Derived -----------------------------------------------------------
    @property
    def provider_names(self) -> list[str]:
        return [p.strip().lower() for p in self.provider_order.split(",") if p.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def resolved_db_path(self) -> Path:
        p = Path(self.db_path)
        if not p.is_absolute():
            p = BACKEND_ROOT / p
        return p

    def monthly_cap_for(self, provider: str) -> int:
        """Local hard cap for a provider. Unknown/free providers are unmetered."""
        return {
            "aerodatabox": self.aerodatabox_monthly_cap,
            "airlabs": self.airlabs_monthly_cap,
        }.get(provider, 0)

    @property
    def terminal_capacity_per_hour(self) -> int:
        """Throughput of a single terminal's security checkpoint."""
        return self.lanes_per_terminal * self.pax_per_lane_per_hour

    @property
    def security_window_hours(self) -> float:
        """How long a passenger is at security, and the co-queuing half-width.

        Two flights' security crowds stop overlapping once their departures
        are further apart than this.
        """
        return (
            self.security_lead_max_minutes - self.security_lead_min_minutes
        ) / 60.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
