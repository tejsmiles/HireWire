"""Injectable clock and business-day math.

Nothing in the engine calls datetime.now() directly: scenarios and tests move time
forward explicitly, which is what makes deadline logic testable.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .config import HOLIDAYS


class Clock:
    def __init__(self, now: datetime | None = None):
        self._fixed = now

    def now(self) -> datetime:
        return self._fixed or datetime.now(timezone.utc)

    def today(self) -> date:
        return self.now().date()

    def set(self, now: datetime) -> None:
        self._fixed = now

    def advance(self, days: int = 0, hours: int = 0, minutes: int = 0) -> None:
        self._fixed = self.now() + timedelta(days=days, hours=hours, minutes=minutes)


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def add_business_days(d: date, n: int) -> date:
    """Move n business days from d (negative = backwards). d itself never counts."""
    step = 1 if n >= 0 else -1
    remaining, cur = abs(n), d
    while remaining:
        cur += timedelta(days=step)
        if is_business_day(cur):
            remaining -= 1
    return cur


def business_days_until(today: date, target: date) -> int:
    """Business days strictly between today and target (0 if target <= today + 1)."""
    if target <= today:
        return 0
    count, cur = 0, today + timedelta(days=1)
    while cur < target:
        count += is_business_day(cur)
        cur += timedelta(days=1)
    return count


def cohort_label(start: date | None) -> str:
    """Cohorts are named by the Monday of the start week."""
    if start is None:
        return ""
    return f"Cohort {(start - timedelta(days=start.weekday())).isoformat()}"
