"""All timestamps are stored/processed in UTC; Sydney time is for display only."""
from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

DISPLAY_TZ = ZoneInfo("Australia/Sydney")


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Return an aware UTC datetime. Naive datetimes are rejected, not guessed."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed; timestamps must be timezone-aware")
    return dt.astimezone(UTC)


def to_display(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(DISPLAY_TZ)
