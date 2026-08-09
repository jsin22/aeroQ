"""Verify a live provider's response shape against our field mappings.

The AeroDataBox field paths in providers/aerodatabox.py were written from
documentation, not from an observed response, and `_first_present` probes
several plausible nestings for each field. This script settles which one is
real, using the smallest possible number of calls.

**Cost: 2 API calls** — one flight resolution, one airport board. It says so
before spending them and requires confirmation.

    cd backend && .venv/bin/python scripts/probe.py B62018 2026-08-10

The raw response is written to scripts/probe-output.json so the mapping can be
corrected without spending anything further.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cache import plan_window  # noqa: E402
from app.config import settings  # noqa: E402
from app.providers.aerodatabox import AeroDataBoxProvider  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402

OUT = Path(__file__).parent / "probe-output.json"


def show_keys(obj, prefix="", depth=0, max_depth=3):
    """Print the response's actual structure, which is the whole point."""
    if depth > max_depth:
        return
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:25]:
            kind = type(v).__name__
            if isinstance(v, (dict, list)):
                print(f"  {'  ' * depth}{prefix}{k}: {kind}")
                show_keys(v, "", depth + 1, max_depth)
            else:
                print(f"  {'  ' * depth}{prefix}{k}: {kind} = {str(v)[:60]!r}")
    elif isinstance(obj, list) and obj:
        print(f"  {'  ' * depth}[{len(obj)} items], first:")
        show_keys(obj[0], "", depth + 1, max_depth)


async def main() -> int:
    flight_no = sys.argv[1] if len(sys.argv) > 1 else "B62018"
    date = sys.argv[2] if len(sys.argv) > 2 else (
        datetime.now() + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    if not settings.aerodatabox_api_key:
        print("AERODATABOX_API_KEY is not set in .env — nothing to probe.")
        return 1

    print(f"Probing AeroDataBox with {flight_no} on {date}.")
    print("This spends 2 API calls (1 resolution + 1 airport board).")
    if input("Continue? [y/N] ").strip().lower() != "y":
        print("Aborted. Nothing was spent.")
        return 0

    provider = AeroDataBoxProvider()
    captured: dict = {}

    try:
        # --- Call 1: flight resolution ---
        print(f"\n{'=' * 70}\nCALL 1 — resolve {flight_no}\n{'=' * 70}")
        try:
            result = await provider.resolve_flight(flight_no, date)
            captured["resolve_raw"] = result.raw
            print("\nRAW STRUCTURE:")
            show_keys(result.raw)
            print(f"\nMAPPED — {len(result.resolutions)} leg(s):")
            for r in result.resolutions:
                print(
                    f"  airport={r.dep_iata!r} terminal={r.dep_terminal!r} "
                    f"(norm {r.dep_terminal_norm!r}) departs={r.dep_time_local} "
                    f"to={r.arr_iata!r}"
                )
            if not result.resolutions:
                print("  NOTHING MAPPED — the field paths need correcting.")
        except ProviderError as exc:
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            return 1

        leg = result.resolutions[0]
        if not leg.dep_iata or not leg.dep_time_local:
            print("\nNo usable departure; skipping call 2.")
            return 1

        # --- Call 2: airport board ---
        start, end = plan_window(leg.dep_time_local)
        print(f"\n{'=' * 70}\nCALL 2 — {leg.dep_iata} board {start} → {end}\n{'=' * 70}")
        board = await provider.fetch_departures(leg.dep_iata, start, end)
        captured["departures_raw"] = board.raw

        print("\nRAW STRUCTURE:")
        show_keys(board.raw)
        print(f"\nMAPPED — {len(board.flights)} flights")

        terminals = {}
        for f in board.flights:
            terminals[f.dep_terminal_norm] = terminals.get(f.dep_terminal_norm, 0) + 1
        print(f"  terminals seen: {terminals}")
        for f in board.flights[:5]:
            print(
                f"  {f.dep_time_local} {f.flight_iata!r} "
                f"terminal={f.dep_terminal!r} -> {f.dep_terminal_norm!r}"
            )

        # --- The verdict ---
        print(f"\n{'=' * 70}\nVERDICT\n{'=' * 70}")
        raw_count = len(
            board.raw.get("departures", []) if isinstance(board.raw, dict) else board.raw or []
        )
        if raw_count and not board.flights:
            print(f"  ✗ {raw_count} departures returned but 0 mapped — fix the paths.")
        elif not board.flights:
            print("  ? Empty board. Try a busier airport or a different time.")
        else:
            print(f"  ✓ {len(board.flights)} of {raw_count} departures mapped.")
            null_terminals = terminals.get(None, 0)
            share = null_terminals / len(board.flights)
            print(
                f"  {'✓' if share < 0.5 else '⚠'} terminals present on "
                f"{(1 - share) * 100:.0f}% of flights"
                + ("" if share < 0.5 else " — the fallback ladder will do heavy lifting")
            )

    finally:
        await provider.aclose()
        if captured:
            OUT.write_text(json.dumps(captured, indent=2, default=str))
            print(f"\nRaw response saved to {OUT}")
            print("Mapping can now be corrected without spending more calls.")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
