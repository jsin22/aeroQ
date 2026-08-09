"""Deterministic fake provider — the default, and the entire development path.

The build consumes zero real API budget because everything up to phase 8 runs
against this. To be useful for that it has to be *deterministic* (the same
query always yields the same board, so tests can assert on it) and *plausible*
(a realistic diurnal curve and terminal split, so the prediction math is
exercised against numbers shaped like reality rather than uniform noise).

Determinism uses hashlib, not `hash()`: Python salts string hashing per
process, so `hash()` would give different boards on every restart.

Behaviours encoded deliberately, for testing downstream phases:

- **Terminal is None when departure is more than 72h out.** This mirrors real
  aviation APIs, where terminal assignment simply does not exist yet, and is
  what exercises the fallback ladder's levels 2-4.
- **Flight numbers divisible by 100 return two legs** (e.g. UA100), to
  exercise multi-leg disambiguation.
- **Flight numbers >= 9000 raise FlightNotFound**, to exercise that path.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta

from ..normalize import (
    FlightResolution,
    NormalizedFlight,
    normalize_terminal,
    parse_flight_number,
)
from .base import FlightNotFound, ProviderResult, ScheduleProvider

# Relative departure volume by hour of day. Two humps — a hard morning bank and
# a broader late-afternoon one — with a near-dead overnight window.
_DIURNAL = [
    0.06, 0.03, 0.02, 0.04, 0.18, 0.55, 0.92, 1.00, 0.95, 0.82, 0.78, 0.84,
    0.88, 0.83, 0.79, 0.86, 0.96, 1.00, 0.93, 0.74, 0.55, 0.38, 0.24, 0.12,
]

# Peak departures per hour, by airport class.
_LARGE_HUB_PEAK = 34
_MEDIUM_PEAK = 14
_SMALL_PEAK = 5

_LARGE_HUBS = {
    "ATL", "LAX", "ORD", "DFW", "DEN", "JFK", "SFO", "SEA", "LAS", "MCO",
    "EWR", "CLT", "PHX", "IAH", "MIA", "LHR", "CDG", "AMS", "FRA", "MAD",
    "BCN", "IST", "DXB", "DOH", "SIN", "HKG", "NRT", "HND", "ICN", "PVG",
    "PEK", "CAN", "BKK", "KUL", "DEL", "BOM", "SYD", "MEL", "YYZ", "MEX",
    "GRU", "JNB", "MUC", "FCO", "LGW", "BOS", "MSP", "DTW", "PHL", "SLC",
}
_MEDIUM = {
    "SJC", "OAK", "SMF", "SNA", "AUS", "BNA", "RDU", "PDX", "SAN", "TPA",
    "MCI", "CLE", "PIT", "CVG", "IND", "CMH", "MKE", "STL", "DUB", "MAN",
    "EDI", "LIS", "OPO", "VIE", "ZRH", "CPH", "ARN", "OSL", "HEL", "PRG",
}

_AIRLINES = [
    ("UA", "United"), ("AA", "American"), ("DL", "Delta"), ("WN", "Southwest"),
    ("AS", "Alaska"), ("B6", "JetBlue"), ("NK", "Spirit"), ("F9", "Frontier"),
    ("BA", "British Airways"), ("LH", "Lufthansa"), ("AF", "Air France"),
    ("KL", "KLM"), ("EK", "Emirates"), ("QR", "Qatar"), ("SQ", "Singapore"),
]

# Airline -> plausible home airports, so resolution returns something coherent
# rather than putting a United flight out of Heathrow.
_AIRLINE_HUBS = {
    "UA": ["SFO", "ORD", "EWR", "IAH", "DEN", "LAX"],
    "AA": ["DFW", "CLT", "ORD", "PHX", "MIA", "PHL"],
    "DL": ["ATL", "DTW", "MSP", "SLC", "JFK", "LAX"],
    "WN": ["LAS", "DEN", "PHX", "BWI", "OAK", "AUS"],
    "AS": ["SEA", "PDX", "ANC", "SFO", "SAN"],
    "B6": ["JFK", "BOS", "FLL", "MCO"],
    "BA": ["LHR", "LGW", "EDI", "MAN"],
    "LH": ["FRA", "MUC", "HAM", "DUS"],
    "AF": ["CDG", "ORY", "NCE", "LYS"],
    "KL": ["AMS"],
    "EK": ["DXB"],
    "QR": ["DOH"],
    "SQ": ["SIN"],
    "NK": ["FLL", "LAS", "DTW"],
    "F9": ["DEN", "LAS", "MCO"],
}

_DEFAULT_HUBS = ["SFO", "LAX", "ORD", "JFK", "ATL", "DFW", "SEA", "DEN"]

# Share of departures a provider reports with no terminal, even close in.
_NULL_TERMINAL_RATE = 0.08

# Beyond this horizon, terminal assignment does not exist yet.
TERMINAL_HORIZON_HOURS = 72


def _seed(*parts: str) -> int:
    """Process-stable seed. `hash()` is salted per process and unusable here."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _rng(*parts: str) -> random.Random:
    return random.Random(_seed(*parts))


def _peak_for(iata: str) -> int:
    if iata in _LARGE_HUBS:
        return _LARGE_HUB_PEAK
    if iata in _MEDIUM:
        return _MEDIUM_PEAK
    # Unknown airports get a stable size from their own code.
    return _SMALL_PEAK + _seed("size", iata) % 6


def _terminals_for(iata: str) -> list[str]:
    """A stable terminal layout per airport."""
    if iata in _LARGE_HUBS:
        n = 3 + _seed("term", iata) % 3          # 3-5
    elif iata in _MEDIUM:
        n = 2 + _seed("term", iata) % 2          # 2-3
    else:
        n = 1
    return [str(i) for i in range(1, n + 1)]


class MockProvider(ScheduleProvider):
    name = "mock"
    supports_flight_lookup = True
    supports_airport_departures = True

    # --- Step 1 -----------------------------------------------------------
    async def resolve_flight(self, flight_no: str, flight_date: str) -> ProviderResult:
        canonical = parse_flight_number(flight_no)
        if not canonical:
            raise FlightNotFound(f"{flight_no!r} is not a valid flight number", self.name)

        airline = "".join(c for c in canonical if c.isalpha())
        number = int("".join(c for c in canonical if c.isdigit()))

        if number >= 9000:
            raise FlightNotFound(f"No flight {canonical} on {flight_date}", self.name)

        rng = _rng("resolve", canonical, flight_date)
        hubs = _AIRLINE_HUBS.get(airline, _DEFAULT_HUBS)

        date = datetime.strptime(flight_date, "%Y-%m-%d")
        legs = 2 if number % 100 == 0 else 1

        resolutions: list[FlightResolution] = []
        chosen: list[str] = []
        for leg in range(legs):
            iata = rng.choice([h for h in hubs if h not in chosen] or hubs)
            chosen.append(iata)

            # Skew toward the banks people actually fly, not uniform across 24h.
            hour = rng.choices(range(24), weights=_DIURNAL, k=1)[0]
            minute = rng.choice([0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55])
            dep = date.replace(hour=hour, minute=minute)
            if leg > 0:
                dep += timedelta(hours=rng.randint(3, 8))

            resolutions.append(
                FlightResolution(
                    flight_no=canonical,
                    flight_date=flight_date,
                    dep_iata=iata,
                    dep_terminal=self._terminal_at(iata, dep, rng),
                    dep_time_local=dep,
                    arr_iata=rng.choice([h for h in _DEFAULT_HUBS if h != iata]),
                    source_provider=self.name,
                )
            )

        return ProviderResult(
            provider=self.name, calls_used=1, resolutions=resolutions, raw={"mock": True}
        )

    def _terminal_at(self, iata: str, dep: datetime, rng: random.Random) -> str | None:
        """None beyond the horizon — the real dead zone this app has to handle."""
        if dep - datetime.now() > timedelta(hours=TERMINAL_HORIZON_HOURS):
            return None
        terminals = _terminals_for(iata)
        if rng.random() < _NULL_TERMINAL_RATE:
            return None
        return rng.choice(terminals)

    # --- Step 2 -----------------------------------------------------------
    async def fetch_departures(
        self, iata: str, window_start: datetime, window_end: datetime
    ) -> ProviderResult:
        iata = iata.upper()
        flights: list[NormalizedFlight] = []

        cursor = window_start.replace(minute=0, second=0, microsecond=0)
        while cursor < window_end:
            flights.extend(self._flights_for_hour(iata, cursor))
            cursor += timedelta(hours=1)

        flights = [f for f in flights if window_start <= f.dep_time_local < window_end]
        flights.sort(key=lambda f: f.dep_time_local)

        # One call regardless of size — a time-windowed endpoint, like
        # AeroDataBox and unlike AirLabs.
        return ProviderResult(
            provider=self.name,
            calls_used=1,
            flights=flights,
            raw={"mock": True, "count": len(flights)},
        )

    def _flights_for_hour(self, iata: str, hour_start: datetime) -> list[NormalizedFlight]:
        rng = _rng("board", iata, hour_start.strftime("%Y-%m-%d %H"))
        peak = _peak_for(iata)
        weight = _DIURNAL[hour_start.hour]
        jitter = rng.uniform(0.82, 1.18)
        count = max(0, round(peak * weight * jitter))

        terminals = _terminals_for(iata)
        # Terminals are not evenly loaded; a stable weighting per airport keeps
        # one terminal busier, as at a real hub with a dominant carrier.
        weights = [1.0 + (_seed("tw", iata, t) % 100) / 100.0 for t in terminals]

        out: list[NormalizedFlight] = []
        for i in range(count):
            minute = rng.randint(0, 59)
            dep = hour_start.replace(minute=minute)
            airline_iata, _ = rng.choice(_AIRLINES)
            number = rng.randint(1, 4999)

            raw_terminal = (
                None
                if rng.random() < _NULL_TERMINAL_RATE
                else rng.choices(terminals, weights=weights, k=1)[0]
            )

            out.append(
                NormalizedFlight(
                    dep_iata=iata,
                    dep_time_local=dep,
                    flight_iata=f"{airline_iata}{number}",
                    airline_iata=airline_iata,
                    dep_terminal=raw_terminal,
                    dep_terminal_norm=normalize_terminal(raw_terminal),
                    status="scheduled",
                    source_provider=self.name,
                )
            )
        return out
