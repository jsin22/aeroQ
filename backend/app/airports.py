"""Offline IATA reference.

Validation policy differs by where the code came from, which matters:

- **Manual entry** is validated strictly against this list. It is user-typed and
  therefore the place typos originate, and a typo that reaches a provider costs
  real budget for a guaranteed-empty answer.
- **Resolution-derived** codes skip validation entirely. The provider already
  told us the airport exists; rejecting it because our bundled list is
  incomplete would break legitimate small airports for no benefit.

That split is why `is_known()` and `validate_manual_iata()` are separate.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .config import BACKEND_ROOT

_IATA_RE = re.compile(r"^[A-Z]{3}$")
_AIRPORTS_FILE = BACKEND_ROOT / "data" / "airports.json"


@lru_cache
def _load() -> dict[str, list[str]]:
    with _AIRPORTS_FILE.open(encoding="utf-8") as fh:
        return json.load(fh)["airports"]


def is_well_formed(iata: str | None) -> bool:
    return bool(iata) and bool(_IATA_RE.match(iata.strip().upper()))


def is_known(iata: str | None) -> bool:
    return bool(iata) and iata.strip().upper() in _load()


def lookup(iata: str | None) -> dict | None:
    if not iata:
        return None
    row = _load().get(iata.strip().upper())
    if not row:
        return None
    icao, name, city, country = row
    return {
        "iata": iata.strip().upper(),
        "icao": icao,
        "name": name,
        "city": city,
        "country": country,
    }


def display_name(iata: str | None, fallback: str | None = None) -> str | None:
    """Human-readable airport name, or None if we genuinely do not have one.

    Returning the IATA code here would render as "POS · POS" in the UI. None
    lets the caller show the code once.
    """
    info = lookup(iata)
    if info:
        return info["name"]
    return fallback or None


class UnknownAirportError(ValueError):
    """Raised for a manually entered code we cannot vouch for."""

    def __init__(self, iata: str, well_formed: bool) -> None:
        self.iata = iata
        self.well_formed = well_formed
        super().__init__(
            f"{iata!r} is not a recognised IATA airport code."
            if well_formed
            else f"{iata!r} is not a valid IATA code (expected three letters)."
        )


def validate_manual_iata(iata: str | None) -> str:
    """Strict check for user-typed codes. Returns the canonical uppercase code."""
    code = (iata or "").strip().upper()
    if not is_well_formed(code):
        raise UnknownAirportError(code, well_formed=False)
    if not is_known(code):
        raise UnknownAirportError(code, well_formed=True)
    return code


def all_airports() -> list[dict]:
    """Every known airport, for the manual form's dropdown."""
    return sorted(
        (
            {"iata": code, "icao": row[0], "name": row[1], "city": row[2], "country": row[3]}
            for code, row in _load().items()
        ),
        key=lambda a: (a["country"], a["city"], a["iata"]),
    )
