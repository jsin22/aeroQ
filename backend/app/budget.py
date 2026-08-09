"""The API budget ledger.

Two ideas do the work:

**Only cache misses are metered.** A cache hit costs nothing and is never
counted, so friends re-checking the same flight are never throttled. Only a
genuinely novel lookup spends anything. Nothing in this module is consulted on
the cache-hit path at all.

**A self-balancing daily allowance:**

    daily_allowance = clamp(remaining_this_month / days_left_in_month, MIN, MAX)

Underuse early and later days get more headroom; a burst throttles the
following days but never to zero, so the month cannot be emptied on day three.
This replaces v1's airport allowlist, which became unworkable once users enter
flight numbers rather than airports — their airports cannot be known ahead of
time.

**Failed attempts count against the local cap.** We cannot reliably tell
whether a timeout reached the provider, and the local caps are deliberately set
below the real free tiers, so over-counting spends a little headroom while
under-counting risks an overage. Calls we *blocked* are recorded but not
counted: they never left the machine.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime

from . import db
from .config import settings

# Statuses that represent budget actually spent.
_SPENT = ("ok", "error")


def is_metered(provider: str) -> bool:
    """Mock (and anything without a cap) is free and stays out of the ledger."""
    return settings.monthly_cap_for(provider) > 0


def days_left_in_month(now: datetime | None = None) -> int:
    now = now or datetime.now()
    _, last_day = calendar.monthrange(now.year, now.month)
    return max(1, last_day - now.day + 1)


def record(
    provider: str,
    endpoint: str,
    status: str,
    calls: int = 1,
    detail: str | None = None,
) -> None:
    """Append to the ledger. Every attempt lands here, including blocked ones,
    so the record explains its own decisions after the fact.

    Unmetered providers (mock, anything without a cap) are recorded for
    observability but always at zero cost — they consume no real quota, and
    counting them would make the daily allowance throttle development.
    """
    if not is_metered(provider):
        calls = 0
    now = datetime.now()
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO api_usage
                (provider, endpoint, called_at, day_key, month_key, status, calls, detail)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                provider,
                endpoint,
                int(now.timestamp()),
                db.day_key(now),
                db.month_key(now),
                status,
                calls,
                detail,
            ),
        )


def monthly_used(provider: str, now: datetime | None = None) -> int:
    with db.connect() as conn:
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(calls), 0) AS n FROM api_usage
            WHERE provider = ? AND month_key = ? AND status IN {_SPENT}
            """,
            (provider, db.month_key(now)),
        ).fetchone()
    return int(row["n"])


def daily_used(now: datetime | None = None) -> int:
    """Pooled across providers — the allowance governs total spend, not per-source."""
    with db.connect() as conn:
        row = conn.execute(
            f"""
            SELECT COALESCE(SUM(calls), 0) AS n FROM api_usage
            WHERE day_key = ? AND status IN {_SPENT}
            """,
            (db.day_key(now),),
        ).fetchone()
    return int(row["n"])


def provider_remaining(provider: str, now: datetime | None = None) -> int:
    cap = settings.monthly_cap_for(provider)
    if cap <= 0:
        return 10**9  # unmetered
    return max(0, cap - monthly_used(provider, now))


def pooled_remaining(providers: list[str], now: datetime | None = None) -> int:
    metered = [p for p in providers if is_metered(p)]
    if not metered:
        return 10**9
    return sum(provider_remaining(p, now) for p in metered)


def daily_allowance(providers: list[str], now: datetime | None = None) -> int:
    """The self-balancing per-day ceiling."""
    metered = [p for p in providers if is_metered(p)]
    if not metered:
        return 10**9

    remaining = pooled_remaining(metered, now)
    per_day = remaining / days_left_in_month(now)
    return int(
        max(
            settings.daily_allowance_min,
            min(settings.daily_allowance_max, per_day),
        )
    )


@dataclass
class BudgetDecision:
    allowed: bool
    reason: str | None = None

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


def provider_reported_remaining(provider: str) -> tuple[int | None, str]:
    """Budget the provider itself last reported, and which counter is scarcest.

    Preferred over local counting where available. AeroDataBox meters "API
    units" alongside requests, and they are not interchangeable — a 12-hour
    board costs 1 request but 2 units, so units run out about four times
    sooner. Counting calls locally measures the wrong quantity; the provider's
    own figure does not drift.
    """
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT units_remaining, requests_remaining, quota_synced_at
            FROM provider_state WHERE provider = ?
            """,
            (provider,),
        ).fetchone()

    if row is None or row["quota_synced_at"] is None:
        return None, "never synced"

    candidates = []
    if row["units_remaining"] is not None:
        candidates.append((row["units_remaining"], "units"))
    if row["requests_remaining"] is not None:
        candidates.append((row["requests_remaining"], "requests"))
    if not candidates:
        return None, "no counters reported"

    return min(candidates)


def check(provider: str, providers: list[str], now: datetime | None = None) -> BudgetDecision:
    """Can `provider` spend at least one call right now?

    Checked *before* dispatch, which is the point: a 429 arrives only after the
    call is already spent, so waiting for one wastes a call per request for the
    rest of the month.

    A provider that paginates may spend more than the one call approved here.
    That overshoot is accepted rather than pre-reserved — the true page count is
    unknowable beforehand, and the next request simply sees the higher total.
    """
    if not is_metered(provider):
        return BudgetDecision(True)

    # The provider's own figure wins when we have one — see
    # provider_reported_remaining().
    reported, counter = provider_reported_remaining(provider)
    if reported is not None and reported <= settings.provider_quota_reserve:
        return BudgetDecision(
            False,
            f"{provider} reports only {reported} {counter} left this month "
            f"(reserve {settings.provider_quota_reserve}); cached airports still work",
        )

    remaining = provider_remaining(provider, now)
    if remaining <= 0:
        return BudgetDecision(
            False, f"{provider} has reached its monthly cap of "
                   f"{settings.monthly_cap_for(provider)}"
        )

    allowance = daily_allowance(providers, now)
    used_today = daily_used(now)
    if used_today >= allowance:
        return BudgetDecision(
            False,
            f"today's allowance of {allowance} calls is spent "
            f"({used_today} used); cached airports still work",
        )

    return BudgetDecision(True)


def snapshot(providers: list[str], now: datetime | None = None) -> dict:
    """Everything /api/quota reports."""
    now = now or datetime.now()
    metered = [p for p in providers if is_metered(p)]
    return {
        "month": db.month_key(now),
        "day": db.day_key(now),
        "days_left_in_month": days_left_in_month(now),
        "providers": [
            {
                "provider": p,
                "monthly_cap": settings.monthly_cap_for(p),
                "used_this_month": monthly_used(p, now),
                "remaining": provider_remaining(p, now),
                "metered": True,
                # What the provider last told us, which supersedes the local
                # count when present.
                "reported_remaining": provider_reported_remaining(p)[0],
                "reported_counter": provider_reported_remaining(p)[1],
            }
            for p in metered
        ]
        + [
            {"provider": p, "monthly_cap": 0, "used_this_month": 0,
             "remaining": None, "metered": False}
            for p in providers
            if not is_metered(p)
        ],
        "pooled_remaining": pooled_remaining(providers, now) if metered else None,
        "used_today": daily_used(now),
        "daily_allowance": daily_allowance(providers, now) if metered else None,
    }
