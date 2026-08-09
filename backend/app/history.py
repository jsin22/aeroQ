"""The self-building corpus.

Every departure board fetched is already stored, so aggregating those snapshots
costs no additional API budget. The corpus builds itself purely as a side
effect of normal use, and yields two things nothing else can provide:

**Terminal inference.** When a provider reports no terminal, a flight seen
departing Terminal 2 on fourteen of its last fifteen observations is a far
better answer than giving up and widening to the whole airport.

**A density baseline.** This addresses a problem deeper than null terminals:
departure boards only extend a few days out, so a flight three weeks away has
no board to filter at all — no amount of terminal data helps. The baseline
answers from typical patterns instead ("based on six previous Fridays"),
clearly labelled as such and never presented as a live reading.

The corpus is empty on day one. Far-future queries therefore fail honestly
until real usage accumulates, which is expected rather than a defect.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import db
from .config import settings
from .normalize import NormalizedFlight

log = logging.getLogger(__name__)

# The whole-airport aggregate, stored alongside per-terminal rows so an
# airport-scope baseline does not require summing across terminals whose
# sample counts differ.
AIRPORT_AGGREGATE = "*"


# --- Writes -----------------------------------------------------------------

def record_board(
    iata: str,
    flights: list[NormalizedFlight],
    window_start: datetime,
    window_end: datetime,
) -> None:
    """Fold a fetched board into the corpus.

    Only whole hours inside the fetched window are recorded: a partially
    covered hour would look quieter than it was and bias the average down.
    """
    iata = iata.upper()
    if not flights:
        return

    per_hour: Counter[tuple[str, str, int, int]] = Counter()

    for f in flights:
        dep = f.dep_time_local
        hour_start = dep.replace(minute=0, second=0, microsecond=0)
        if hour_start < window_start or hour_start + timedelta(hours=1) > window_end:
            continue

        date_key = dep.strftime("%Y-%m-%d")
        per_hour[(date_key, AIRPORT_AGGREGATE, dep.weekday(), dep.hour)] += 1
        if f.dep_terminal_norm:
            per_hour[(date_key, f.dep_terminal_norm, dep.weekday(), dep.hour)] += 1

    # Record an explicit zero for every whole hour in the window that saw no
    # flights. Without this, "we observed this hour and it was empty" and "we
    # have never observed this hour" are indistinguishable, and the baseline
    # cannot tell a genuinely quiet 3am from a gap in its own coverage.
    terminals_seen = {f.dep_terminal_norm for f in flights if f.dep_terminal_norm}
    cursor = window_start.replace(minute=0, second=0, microsecond=0)
    if cursor < window_start:
        cursor += timedelta(hours=1)
    while cursor + timedelta(hours=1) <= window_end:
        date_key = cursor.strftime("%Y-%m-%d")
        for terminal in {AIRPORT_AGGREGATE, *terminals_seen}:
            per_hour.setdefault(
                (date_key, terminal, cursor.weekday(), cursor.hour), 0
            )
        cursor += timedelta(hours=1)

    if not per_hour:
        return

    with db.connect() as conn:
        for (date_key, terminal, dow, hour), count in per_hour.items():
            row = conn.execute(
                """
                SELECT avg_flights, sample_count, last_sample_date
                FROM terminal_density_history
                WHERE iata=? AND terminal_norm=? AND day_of_week=? AND hour=?
                """,
                (iata, terminal, dow, hour),
            ).fetchone()

            if row is None:
                conn.execute(
                    """
                    INSERT INTO terminal_density_history
                        (iata, terminal_norm, day_of_week, hour,
                         avg_flights, sample_count, last_sample_date)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (iata, terminal, dow, hour, float(count), 1, date_key),
                )
                continue

            if row["last_sample_date"] == date_key:
                # Same calendar day seen again — a refetch, not a new sample.
                continue

            n = row["sample_count"]
            new_avg = (row["avg_flights"] * n + count) / (n + 1)
            conn.execute(
                """
                UPDATE terminal_density_history
                SET avg_flights=?, sample_count=?, last_sample_date=?
                WHERE iata=? AND terminal_norm=? AND day_of_week=? AND hour=?
                """,
                (new_avg, n + 1, date_key, iata, terminal, dow, hour),
            )


def record_terminals(flights: list[NormalizedFlight]) -> None:
    """Accumulate which terminal each flight number actually departs from."""
    observations = Counter(
        (f.flight_iata, f.dep_terminal_norm)
        for f in flights
        if f.flight_iata and f.dep_terminal_norm
    )
    if not observations:
        return

    now = db.now_ts()
    with db.connect() as conn:
        for (flight_iata, terminal), count in observations.items():
            conn.execute(
                """
                INSERT INTO flight_terminal_history
                    (flight_iata, terminal_norm, observed_count, last_seen)
                VALUES (?,?,?,?)
                ON CONFLICT(flight_iata, terminal_norm) DO UPDATE SET
                    observed_count = observed_count + excluded.observed_count,
                    last_seen = excluded.last_seen
                """,
                (flight_iata, terminal, count, now),
            )


# --- Reads ------------------------------------------------------------------

@dataclass
class TerminalGuess:
    terminal: str
    observed: int
    total: int

    @property
    def confidence_text(self) -> str:
        return f"usually departs Terminal {self.terminal} ({self.observed} of {self.total} observations)"


def modal_terminal(flight_iata: str | None) -> TerminalGuess | None:
    """The terminal a flight most often departs from, if we have seen it."""
    if not flight_iata:
        return None

    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT terminal_norm, observed_count FROM flight_terminal_history
            WHERE flight_iata = ? ORDER BY observed_count DESC
            """,
            (flight_iata,),
        ).fetchall()

    if not rows:
        return None
    total = sum(r["observed_count"] for r in rows)
    return TerminalGuess(rows[0]["terminal_norm"], rows[0]["observed_count"], total)


@dataclass
class BaselineEstimate:
    flights: float
    sample_count: int
    day_of_week: int
    terminal: str

    @property
    def weekday_name(self) -> str:
        return [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        ][self.day_of_week]

    @property
    def description(self) -> str:
        return f"based on {self.sample_count} previous {self.weekday_name}s"


def baseline_for_window(
    iata: str,
    terminal_norm: str | None,
    window_start: datetime,
    window_end: datetime,
) -> BaselineEstimate | None:
    """Typical flight count in a window, from accumulated history.

    Returns None below `min_samples_for_baseline` — a guess from one prior
    observation would carry false authority.
    """
    iata = iata.upper()
    terminal = terminal_norm or AIRPORT_AGGREGATE

    # A rush window rarely aligns to the hour: [12:30, 14:30) covers half of
    # hour 12, all of 13, and half of 14. Each hour's average is weighted by
    # how much of it the window actually spans, or a 2-hour window would be
    # summed from three whole hours and over-count by ~50%.
    hours: list[tuple[int, int, float]] = []
    cursor = window_start.replace(minute=0, second=0, microsecond=0)
    while cursor < window_end:
        hour_end = cursor + timedelta(hours=1)
        overlap = (
            min(hour_end, window_end) - max(cursor, window_start)
        ).total_seconds() / 3600.0
        if overlap > 0:
            hours.append((cursor.weekday(), cursor.hour, overlap))
        cursor = hour_end
    if not hours:
        return None

    total = 0.0
    min_samples = None
    with db.connect() as conn:
        for dow, hour, overlap in hours:
            row = conn.execute(
                """
                SELECT avg_flights, sample_count FROM terminal_density_history
                WHERE iata=? AND terminal_norm=? AND day_of_week=? AND hour=?
                """,
                (iata, terminal, dow, hour),
            ).fetchone()
            if row is None:
                # Genuinely unobserved — zeros are recorded explicitly, so a
                # missing row means a gap in coverage, not a quiet hour.
                return None
            total += row["avg_flights"] * overlap
            min_samples = (
                row["sample_count"]
                if min_samples is None
                else min(min_samples, row["sample_count"])
            )

    if min_samples is None or min_samples < settings.min_samples_for_baseline:
        return None

    return BaselineEstimate(
        flights=total,
        sample_count=min_samples,
        day_of_week=window_start.weekday(),
        terminal=terminal,
    )


def corpus_stats() -> dict:
    with db.connect() as conn:
        density = conn.execute(
            "SELECT COUNT(*) c, COALESCE(MAX(sample_count),0) m FROM terminal_density_history"
        ).fetchone()
        terminals = conn.execute(
            "SELECT COUNT(DISTINCT flight_iata) c FROM flight_terminal_history"
        ).fetchone()
    return {
        "density_slots": density["c"],
        "max_samples_per_slot": density["m"],
        "flights_with_terminal_history": terminals["c"],
        "min_samples_for_baseline": settings.min_samples_for_baseline,
    }
