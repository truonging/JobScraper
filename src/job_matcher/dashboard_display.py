"""Presentation helpers that do not affect indexed content or screening."""

from datetime import UTC, datetime, timedelta, timezone

PST = timezone(timedelta(hours=-8), name="PST")


def format_posted_date(value: str | None) -> str:
    """Publication/creation date in UTC, never replaced by discovery time."""
    try:
        instant = datetime.fromisoformat(value or "")
        if instant.utcoffset() is not None:
            return instant.astimezone(UTC).strftime("%Y-%m-%d")
    except ValueError:
        pass
    return "Unknown"


def format_pst_timestamp(value: str | None) -> str:
    """Display an aware catalog timestamp in fixed UTC-8 Pacific Standard Time."""
    if not value:
        return "Unavailable"
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return "Unavailable"
    if instant.utcoffset() is None:
        return "Unavailable"
    return instant.astimezone(PST).strftime("%Y-%m-%d %I:%M %p")
