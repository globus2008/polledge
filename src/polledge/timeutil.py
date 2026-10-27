"""Time handling: the database stores naive UTC; people see and enter local (Europe/Prague) time.

Runs on different machines (a home PC in CET, GitHub Actions and Streamlit Cloud in UTC) share one
database, so local time must never be written to it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DISPLAY_TZ = "Europe/Prague"


def utcnow() -> datetime:
    """Current time as naive UTC (the storage convention)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _zone(name: str = DISPLAY_TZ):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:  # Windows without the tzdata package
        return UTC


def to_local(value: datetime | None, tz: str = DISPLAY_TZ) -> datetime | None:
    """Naive UTC from the database -> naive local time for display."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC).astimezone(_zone(tz)).replace(tzinfo=None)


def local_to_utc(value: datetime, tz: str = DISPLAY_TZ) -> datetime:
    """Naive local time entered by a person (odds files, forms) -> naive UTC for the database."""
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value.replace(tzinfo=_zone(tz)).astimezone(UTC).replace(tzinfo=None)
