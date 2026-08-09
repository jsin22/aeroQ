"""AirLabs — secondary provider.

Kept for two reasons despite ranking below AeroDataBox on calls-per-picture:
it is genuine failover when the primary is exhausted, and at a small airport a
single page *is* the complete picture, so it costs exactly the same as a
time-windowed endpoint there.

The pagination cost is the thing this module must be honest about.
`ProviderResult.calls_used` reports pages actually fetched, so the budget
ledger reflects that one logical request cost six calls at a large hub. Hiding
that behind the interface would make every budget decision downstream wrong.

`MAX_PAGES` bounds the damage: without it, a single request against a very
large airport could quietly consume a meaningful slice of the monthly quota.
Hitting the bound returns a partial result flagged as such rather than
pretending the picture is complete — an under-counted board would silently
under-predict the wait, which is the dangerous direction to be wrong in.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

from ..config import settings
from ..normalize import (
    FlightResolution,
    NormalizedFlight,
    normalize_terminal,
    parse_flight_number,
    parse_local_time,
)
from .base import (
    FlightNotFound,
    ProviderAuthError,
    ProviderQuotaExceeded,
    ProviderResult,
    ProviderTransientError,
    ScheduleProvider,
)

log = logging.getLogger(__name__)

BASE_URL = "https://airlabs.co/api/v9"
PAGE_SIZE = 100
MAX_PAGES = 6


class AirLabsProvider(ScheduleProvider):
    name = "airlabs"
    supports_flight_lookup = True
    supports_airport_departures = True

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.airlabs_api_key
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL, timeout=httpx.Timeout(15.0, connect=8.0)
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, path: str, params: dict) -> Any:
        if not self.is_configured:
            raise ProviderAuthError("AIRLABS_API_KEY is not set", self.name)

        params = {**params, "api_key": self.api_key}
        try:
            resp = await self._get_client().get(path, params=params)
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(f"timeout: {exc}", self.name) from exc
        except httpx.HTTPError as exc:
            raise ProviderTransientError(f"connection error: {exc}", self.name) from exc

        if resp.status_code == 429:
            raise ProviderQuotaExceeded("rate limit / quota exceeded", self.name)
        if resp.status_code >= 500:
            raise ProviderTransientError(f"upstream {resp.status_code}", self.name)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderTransientError(f"malformed JSON: {exc}", self.name) from exc

        # AirLabs reports failures in the body with HTTP 200, so status codes
        # alone are not a sufficient error signal here.
        if isinstance(payload, dict) and "error" in payload:
            err = payload["error"] or {}
            key = str(err.get("code", "")).lower()
            message = str(err.get("message", err))
            if "limit" in key or "quota" in key or "limit" in message.lower():
                raise ProviderQuotaExceeded(message, self.name)
            if "key" in key or "auth" in key:
                raise ProviderAuthError(message, self.name)
            raise ProviderTransientError(message, self.name)

        return payload

    # --- Step 1 -----------------------------------------------------------
    async def resolve_flight(self, flight_no: str, flight_date: str) -> ProviderResult:
        canonical = parse_flight_number(flight_no)
        if not canonical:
            raise FlightNotFound(f"{flight_no!r} is not a valid flight number", self.name)

        payload = await self._request("/schedules", {"flight_iata": canonical})
        rows = payload.get("response", []) if isinstance(payload, dict) else []

        resolutions = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("dep_iata"):
                continue
            dep_time = parse_local_time(row.get("dep_time"))
            # /schedules is a near-term board; drop rows for other dates.
            if dep_time and dep_time.strftime("%Y-%m-%d") != flight_date:
                continue
            resolutions.append(
                FlightResolution(
                    flight_no=canonical,
                    flight_date=flight_date,
                    dep_iata=str(row["dep_iata"]),
                    dep_terminal=row.get("dep_terminal"),
                    dep_time_local=dep_time,
                    arr_iata=row.get("arr_iata"),
                    source_provider=self.name,
                    extra={"status": row.get("status")},
                )
            )

        if not resolutions:
            raise FlightNotFound(f"No flight {canonical} on {flight_date}", self.name)

        return ProviderResult(
            provider=self.name, calls_used=1, resolutions=resolutions, raw=payload
        )

    # --- Step 2 -----------------------------------------------------------
    async def fetch_departures(
        self, iata: str, window_start: datetime, window_end: datetime
    ) -> ProviderResult:
        iata = iata.upper()
        rows: list[dict] = []
        calls = 0
        partial = False
        partial_reason = None

        for page in range(MAX_PAGES):
            payload = await self._request(
                "/schedules",
                {"dep_iata": iata, "limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
            )
            calls += 1
            batch = payload.get("response", []) if isinstance(payload, dict) else []
            rows.extend(r for r in batch if isinstance(r, dict))

            if len(batch) < PAGE_SIZE:
                break
        else:
            # Loop ran to MAX_PAGES without a short page: more data remains.
            partial = True
            partial_reason = (
                f"stopped at {MAX_PAGES} pages to bound cost; board may be incomplete"
            )
            log.warning("airlabs: %s hit the %d-page cap", iata, MAX_PAGES)

        flights = []
        for row in rows:
            dep_time = parse_local_time(row.get("dep_time"))
            if dep_time is None or not (window_start <= dep_time < window_end):
                continue
            raw_terminal = row.get("dep_terminal")
            flights.append(
                NormalizedFlight(
                    dep_iata=iata,
                    dep_time_local=dep_time,
                    flight_iata=(
                        parse_flight_number(row.get("flight_iata"))
                        if row.get("flight_iata")
                        else None
                    ),
                    airline_iata=row.get("airline_iata"),
                    dep_terminal=str(raw_terminal) if raw_terminal is not None else None,
                    dep_terminal_norm=normalize_terminal(raw_terminal),
                    dep_time_utc=row.get("dep_time_utc"),
                    status=row.get("status"),
                    source_provider=self.name,
                )
            )
        flights.sort(key=lambda f: f.dep_time_local)

        return ProviderResult(
            provider=self.name,
            calls_used=calls,          # pages, not requests — the honest number
            flights=flights,
            raw={"rows": len(rows), "pages": calls},
            partial=partial,
            partial_reason=partial_reason,
        )
