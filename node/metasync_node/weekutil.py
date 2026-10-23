"""ISO-8601 week helpers (pure). Weeks start Monday; week 1 contains Jan 4."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone


def iso_week_string(when: datetime | date) -> str:
    iso = when.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def days_between(from_iso: str, to: datetime) -> int:
    from_dt = datetime.fromisoformat(from_iso)
    if from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=timezone.utc)
    return (to - from_dt).days


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def week_start_utc(when: datetime) -> datetime:
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc)
    monday = when.date() - timedelta(days=when.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)


def closed_week_start(when: datetime) -> datetime:
    """Monday 00:00 UTC of the ISO week BEFORE the one containing ``when``:
    the week a report sent now describes (DESIGN-NODE.md §12)."""
    return week_start_utc(when) - timedelta(days=7)
