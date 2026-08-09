"""Shared normalization: the boundary where provider-shaped data stops.

Terminal designators are a property of aviation, not of any vendor, so the
normalizer is shared rather than per-provider. Providers own only the mapping
from their JSON into `NormalizedFlight`; the terminal string itself is
normalized here so every provider produces identical values for identical
real-world terminals.

Note the limit of this: normalization fixes *format* drift ("T1" vs
"Terminal 1"), not *semantic* mismatch. If one provider reports a terminal
where another reports a concourse, both normalize cleanly but mean different
things. That is handled structurally instead — a cached airport picture is
always sourced from exactly one provider (see cache.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

LOCAL_TIME_FMT = "%Y-%m-%d %H:%M"

# Values that mean "no terminal", across providers.
_NULL_TERMINAL_TOKENS = {
    "", "-", "--", "N/A", "NA", "NIL", "NULL", "NONE", "TBD", "TBA", "?", "UNKNOWN",
}

# Leading nouns that carry no information once stripped. The prefix must be
# followed by a word boundary or a digit: the boundary handles "TERMINAL 1" and
# "TERMINAL-1", the digit lookahead handles the unseparated "TERMINAL1". Without
# the lookahead the two spellings normalize differently; without the boundary a
# terminal genuinely named e.g. "PIERRE" would be truncated.
_TERMINAL_PREFIX_RE = re.compile(
    r"^(TERMINALS?|TERM|CONCOURSE|HALL|PIER)(?:\b|(?=\d))[\s.:#\-]*"
)

# "T1", "T 1", "T-2A" -> the numeric part. Only applied when a digit follows,
# so genuine letter terminals ("T" as a name) are left alone.
_T_NUMERIC_RE = re.compile(r"^T[\s.\-]*(\d{1,2}[A-Z]?)$")

# Airline designator + number: UA123, UA 123, ua-123, BAW1476.
_FLIGHT_NO_RE = re.compile(r"^([A-Z]{2,3})[\s\-]*(\d{1,4})([A-Z]?)$")


def normalize_terminal(raw: str | None) -> str | None:
    """Collapse a provider's terminal string to a canonical form.

    >>> normalize_terminal("Terminal 1"), normalize_terminal("T1"), normalize_terminal("1")
    ('1', '1', '1')
    >>> normalize_terminal("Concourse A"), normalize_terminal("a")
    ('A', 'A')
    >>> normalize_terminal("N/A") is None
    True
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = str(raw)

    s = re.sub(r"\s+", " ", raw).strip().upper()
    if s in _NULL_TERMINAL_TOKENS:
        return None

    s = _TERMINAL_PREFIX_RE.sub("", s).strip()
    if s in _NULL_TERMINAL_TOKENS:
        return None

    m = _T_NUMERIC_RE.match(s)
    if m:
        s = m.group(1)

    # Drop decorative punctuation but keep alphanumerics that carry meaning.
    s = re.sub(r"[^\w]", "", s)
    if not s or s in _NULL_TERMINAL_TOKENS:
        return None

    # Strip insignificant leading zeros ("01" -> "1") without eating "0" itself.
    if s.isdigit():
        s = str(int(s))

    return s


def parse_flight_number(raw: str | None) -> str | None:
    """Canonicalize a flight number, or return None if it is malformed.

    Validation happens before any provider call so typos never cost budget.

    >>> parse_flight_number("ua 123"), parse_flight_number("UA-123")
    ('UA123', 'UA123')
    >>> parse_flight_number("hello") is None
    True
    """
    if not raw or not isinstance(raw, str):
        return None

    s = re.sub(r"\s+", " ", raw).strip().upper()
    m = _FLIGHT_NO_RE.match(s)
    if not m:
        return None

    airline, number, suffix = m.groups()
    # Providers key on the unpadded number: "UA0123" and "UA123" are one flight.
    return f"{airline}{int(number)}{suffix}"


def airline_from_flight_number(flight_no: str) -> str | None:
    m = _FLIGHT_NO_RE.match(flight_no.upper())
    return m.group(1) if m else None


def parse_local_time(raw: str | None) -> datetime | None:
    """Accept the several shapes providers use for a local departure time.

    All are treated as naive local airport time. Any timezone suffix is
    discarded rather than converted: comparisons only ever happen between two
    times at the same airport, so the offset cancels, and carrying tz-aware
    values here would invite accidental UTC/local mixing.
    """
    if not raw:
        return None
    s = str(raw).strip().replace("T", " ")

    # Drop a trailing offset or Z ("2026-08-10 14:30+02:00" -> "2026-08-10 14:30").
    s = re.sub(r"\s*(Z|[+-]\d{2}:?\d{2})$", "", s).strip()

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def format_local_time(dt: datetime) -> str:
    return dt.strftime(LOCAL_TIME_FMT)


@dataclass
class NormalizedFlight:
    """One departure, in the single internal shape the database stores."""

    dep_iata: str
    dep_time_local: datetime
    flight_iata: str | None = None
    airline_iata: str | None = None
    dep_terminal: str | None = None          # as the provider gave it
    dep_terminal_norm: str | None = None     # normalize_terminal() output
    dep_time_utc: str | None = None
    status: str | None = None
    source_provider: str = "unknown"

    def __post_init__(self) -> None:
        self.dep_iata = self.dep_iata.upper()
        if self.dep_terminal_norm is None:
            self.dep_terminal_norm = normalize_terminal(self.dep_terminal)


@dataclass
class FlightResolution:
    """Step 1 output: where and when a given flight number departs."""

    flight_no: str
    flight_date: str                          # 'YYYY-MM-DD'
    dep_iata: str | None = None
    dep_terminal: str | None = None
    dep_terminal_norm: str | None = None
    dep_time_local: datetime | None = None
    arr_iata: str | None = None
    source_provider: str = "unknown"
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.dep_iata:
            self.dep_iata = self.dep_iata.upper()
        if self.dep_terminal_norm is None:
            self.dep_terminal_norm = normalize_terminal(self.dep_terminal)
