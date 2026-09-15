"""
Application wall-clock helpers.

Business TIMESTAMP columns are interpreted in Asia/Shanghai (+08:00).
Use now_local() for DB writes/filters; keep UTC for JWT/protocol clocks.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

APP_TZ = ZoneInfo("Asia/Shanghai")
MYSQL_TIME_ZONE = "+08:00"


def now_local() -> datetime:
    """Naive Beijing wall-clock datetime for TIMESTAMP bind/compare."""
    return datetime.now(APP_TZ).replace(tzinfo=None)


def today_local() -> date:
    """Beijing calendar date for due/plan comparisons (never date.today())."""
    return now_local().date()


def now_utc() -> datetime:
    """UTC wall-clock (naive) for non-DB protocol clocks when needed."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
