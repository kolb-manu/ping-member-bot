"""All times are stored as UTC ISO strings and handled as aware datetimes."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; times must be timezone-aware")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_local_date(dt: datetime, tz: ZoneInfo) -> str:
    """Date only, for the first-charge date, e.g. '15 Nov 2026'."""
    local = dt.astimezone(tz)
    return f"{local.day} {local:%b %Y}"

def format_local(dt: datetime, tz: ZoneInfo) -> str:
    """For showing times to users, e.g. '3 Nov 2026, 18:30'."""
    local = dt.astimezone(tz)
    return f"{local.day} {local:%b %Y, %H:%M}"
