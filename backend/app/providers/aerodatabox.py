"""AeroDataBox, via RapidAPI.

The primary provider, chosen on **calls per complete airport picture** rather
than raw monthly quota (BUILD_PLAN.md §3). Its departures endpoint is
time-windowed: one call returns the whole block whether the airport has 10
departures or 800. A count-paginated endpoint instead charges in proportion to
airport size — exactly the wrong scaling, since large hubs are both the most
expensive to fetch and the most interesting to predict.

⚠ **Response shapes below are written from documentation, not from an observed
response.** They are deliberately defensive: `_first_present` probes several
plausible paths for each field rather than assuming one. Phase 8 makes a single
deliberate live call per provider to confirm. Because `raw_payload` is stored
for every fetch, a mapping correction can be applied to already-cached data
without spending budget to re-fetch.
"""

from __future__ import annotations

import asyncio
import logging
import time
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

_TIME_FMT = "%Y-%m-%dT%H:%M"


def _first_present(obj: Any, *paths: tuple[str, ...]) -> Any:
    """Return the first path that resolves to a non-empty value.

    Defensive against the exact nesting differing from the docs — see the
    module docstring.
    """
    for path in paths:
        cur = obj
        for key in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(key)
            if cur is None:
                break
        if cur not in (None, "", []):
            return cur
    return None


class AeroDataBoxProvider(ScheduleProvider):
    name = "aerodatabox"
    supports_flight_lookup = True
    supports_airport_departures = True

    def __init__(self, api_key: str | None = None, api_host: str | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.aerodatabox_api_key
        self.api_host = api_host or settings.aerodatabox_api_host
        self._client: httpx.AsyncClient | None = None
        # Serialises requests and spaces them out; see _throttle().
        self._rate_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self.last_quota: dict[str, int] | None = None

    async def _throttle(self) -> None:
        """Space requests out to stay under the per-second rate limit.

        Observed behaviour: two calls issued back to back return 429, while the
        same second call succeeds after a short pause. Without this, ordinary
        use — resolve a flight, then immediately fetch that airport's board —
        trips the limit on essentially every cold request.
        """
        interval = settings.aerodatabox_min_request_interval
        if interval <= 0:
            return
        async with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)
            self._last_request_at = time.monotonic()

    @staticmethod
    def _read_quota(headers) -> dict[str, int]:
        """Pull RapidAPI's budget counters out of the response headers.

        Two independent budgets are reported, and they are not interchangeable:
        a 12-hour board costs 1 request but 2 units, so units run out roughly
        four times sooner. Whichever is scarcer is the real limit.
        """
        out: dict[str, int] = {}
        pairs = (
            ("units_remaining", "x-ratelimit-api-units-remaining"),
            ("units_limit", "x-ratelimit-api-units-limit"),
            ("requests_remaining", "x-ratelimit-requests-remaining"),
            ("requests_limit", "x-ratelimit-requests-limit"),
        )
        for key, header in pairs:
            value = headers.get(header)
            if value is not None:
                try:
                    out[key] = int(value)
                except (TypeError, ValueError):
                    continue
        return out

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"https://{self.api_host}",
                headers={
                    "x-rapidapi-key": self.api_key,
                    "x-rapidapi-host": self.api_host,
                },
                timeout=httpx.Timeout(15.0, connect=8.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, path: str, params: dict | None = None, _retry: bool = True) -> Any:
        if not self.is_configured:
            raise ProviderAuthError("AERODATABOX_API_KEY is not set", self.name)

        await self._throttle()
        try:
            resp = await self._get_client().get(path, params=params or {})
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(f"timeout: {exc}", self.name) from exc
        except httpx.HTTPError as exc:
            raise ProviderTransientError(f"connection error: {exc}", self.name) from exc

        quota = self._read_quota(resp.headers)
        if quota:
            self.last_quota = quota

        if resp.status_code == 429:
            # RapidAPI returns 429 for two completely different conditions, and
            # conflating them is expensive: a per-second rate limit is transient
            # and clears in a moment, whereas quota exhaustion is sticky for the
            # month. Treating a burst as exhaustion would disable the provider
            # for weeks over a momentary spike. The headers tell them apart —
            # budget left means it was the rate limit.
            units_left = quota.get("units_remaining")
            requests_left = quota.get("requests_remaining")
            has_budget = (units_left is None or units_left > 0) and (
                requests_left is None or requests_left > 0
            )

            if has_budget:
                if _retry:
                    log.info("aerodatabox: rate limited, backing off once")
                    await asyncio.sleep(settings.aerodatabox_min_request_interval * 2)
                    return await self._request(path, params, _retry=False)
                raise ProviderTransientError(
                    f"rate limited (units left: {units_left}, requests left: {requests_left})",
                    self.name,
                )

            raise ProviderQuotaExceeded(
                f"monthly quota exhausted (units {units_left}, requests {requests_left})",
                self.name,
            )
        if resp.status_code in (401, 403):
            raise ProviderAuthError(f"auth rejected ({resp.status_code})", self.name)
        if resp.status_code == 404:
            raise FlightNotFound("not found", self.name)
        if resp.status_code >= 500:
            raise ProviderTransientError(f"upstream {resp.status_code}", self.name)
        if resp.status_code >= 400:
            raise ProviderTransientError(
                f"unexpected {resp.status_code}: {resp.text[:200]}", self.name
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderTransientError(f"malformed JSON: {exc}", self.name) from exc

    # --- Step 1 -----------------------------------------------------------
    async def resolve_flight(self, flight_no: str, flight_date: str) -> ProviderResult:
        canonical = parse_flight_number(flight_no)
        if not canonical:
            raise FlightNotFound(f"{flight_no!r} is not a valid flight number", self.name)

        payload = await self._request(
            f"/flights/number/{canonical}/{flight_date}",
            {"withAircraftImage": "false", "withLocation": "false"},
        )

        items = payload if isinstance(payload, list) else payload.get("flights", []) or []
        resolutions = [self._map_resolution(i, canonical, flight_date) for i in items]
        resolutions = [r for r in resolutions if r and r.dep_iata]

        if not resolutions:
            raise FlightNotFound(f"No flight {canonical} on {flight_date}", self.name)

        return ProviderResult(
            provider=self.name, calls_used=1, resolutions=resolutions,
            raw=payload, quota=self.last_quota,
        )

    def _map_resolution(
        self, item: dict, flight_no: str, flight_date: str
    ) -> FlightResolution | None:
        if not isinstance(item, dict):
            return None
        dep = item.get("departure") if isinstance(item.get("departure"), dict) else {}

        iata = _first_present(
            item,
            ("departure", "airport", "iata"),
            ("movement", "airport", "iata"),
            ("origin", "iata"),
        )
        if not iata:
            return None

        raw_time = _first_present(
            item,
            ("departure", "scheduledTime", "local"),
            ("movement", "scheduledTime", "local"),
            ("departure", "scheduledTimeLocal"),
        )
        terminal = _first_present(
            item, ("departure", "terminal"), ("movement", "terminal")
        )
        arr = _first_present(
            item, ("arrival", "airport", "iata"), ("destination", "iata")
        )

        return FlightResolution(
            flight_no=flight_no,
            flight_date=flight_date,
            dep_iata=str(iata),
            dep_terminal=str(terminal) if terminal is not None else None,
            dep_time_local=parse_local_time(raw_time),
            arr_iata=str(arr) if arr else None,
            source_provider=self.name,
            extra={
                "status": item.get("status"),
                "gate": dep.get("gate"),
                # The provider names airports our bundled list does not cover
                # (small and regional fields), so keep it rather than falling
                # back to showing the bare IATA code twice.
                "dep_airport_name": _first_present(
                    item,
                    ("departure", "airport", "name"),
                    ("movement", "airport", "name"),
                ),
            },
        )

    # --- Step 2 -----------------------------------------------------------
    async def fetch_departures(
        self, iata: str, window_start: datetime, window_end: datetime
    ) -> ProviderResult:
        iata = iata.upper()
        payload = await self._request(
            f"/flights/airports/iata/{iata}"
            f"/{window_start.strftime(_TIME_FMT)}/{window_end.strftime(_TIME_FMT)}",
            {
                "withLeg": "false",
                "direction": "Departure",
                "withCancelled": "true",
                "withCodeshared": "false",  # codeshares double-count passengers
                "withCargo": "false",       # no security queue from a freighter
                "withPrivate": "false",
                "withLocation": "false",
            },
        )

        items = (
            payload.get("departures", [])
            if isinstance(payload, dict)
            else (payload or [])
        )
        flights = [self._map_flight(i, iata) for i in items]
        flights = [f for f in flights if f is not None]

        if items and not flights:
            log.warning(
                "aerodatabox: %d departures returned but none mapped for %s — "
                "response shape likely differs from the assumed one (BUILD_PLAN §12.1)",
                len(items),
                iata,
            )

        # One call for the whole window, regardless of flight count. Verified
        # live: a 12-hour JFK block returned 391 departures for 1 request.
        return ProviderResult(
            provider=self.name, calls_used=1, flights=flights,
            raw=payload, quota=self.last_quota,
        )

    def _map_flight(self, item: dict, iata: str) -> NormalizedFlight | None:
        if not isinstance(item, dict):
            return None

        raw_time = _first_present(
            item,
            ("departure", "scheduledTime", "local"),
            ("movement", "scheduledTime", "local"),
            ("scheduledTime", "local"),
            ("departure", "scheduledTimeLocal"),
        )
        dep_time = parse_local_time(raw_time)
        if dep_time is None:
            return None

        terminal = _first_present(
            item, ("departure", "terminal"), ("movement", "terminal"), ("terminal",)
        )
        raw_utc = _first_present(
            item,
            ("departure", "scheduledTime", "utc"),
            ("movement", "scheduledTime", "utc"),
            ("scheduledTime", "utc"),
        )
        number = item.get("number") or item.get("callSign")
        airline = _first_present(item, ("airline", "iata"))

        return NormalizedFlight(
            dep_iata=iata,
            dep_time_local=dep_time,
            flight_iata=parse_flight_number(number) if number else None,
            airline_iata=str(airline) if airline else None,
            dep_terminal=str(terminal) if terminal is not None else None,
            dep_terminal_norm=normalize_terminal(terminal),
            dep_time_utc=str(raw_utc) if raw_utc else None,
            status=item.get("status"),
            source_provider=self.name,
        )
