"""Canonical review instants and public labels for Dash's follow-through."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "America/New_York"
TIME_PROPERTIES = {
    "review_at": {
        "type": "string",
        "description": (
            "Exact review time in ISO 8601 with an explicit UTC offset, "
            "e.g. 2026-11-15T04:30:00+00:00."
        ),
    },
    "review_timezone": {
        "type": "string",
        "description": "IANA timezone for the public review time; defaults to America/New_York.",
    },
    "review_minutes": {"type": "integer", "minimum": 30, "maximum": 1440},
}


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Review time needs an explicit UTC offset.")
    return parsed.astimezone(UTC)


def propose(args: dict, at: datetime) -> dict:
    """Resolve absolute or legacy relative input to one aware UTC instant."""
    minutes = args.get("review_minutes")
    if minutes is not None and (type(minutes) is not int or not 30 <= minutes <= 1440):
        raise ValueError("Choose a review time from 30 minutes to one day.")
    value = args.get("review_at")
    if value is not None:
        if not isinstance(value, str):
            raise ValueError("Use an ISO 8601 review time with an explicit UTC offset.")
        try:
            review = instant(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Use an ISO 8601 review time with an explicit UTC offset.") from exc
        if minutes is not None and review != at + timedelta(minutes=minutes):
            raise ValueError("review_at and review_minutes must identify the same time; use one.")
    elif minutes is not None:
        review = at + timedelta(minutes=minutes)
    else:
        raise ValueError("Supply review_at with a UTC offset, or legacy review_minutes.")
    if not at + timedelta(minutes=30) <= review <= at + timedelta(days=1):
        raise ValueError("Choose a review time from 30 minutes to one day in the future.")
    timezone = args.get("review_timezone", DEFAULT_TIMEZONE)
    try:
        ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Choose a valid IANA review_timezone, such as America/New_York.") from exc
    result = {"review_at": review.astimezone(UTC).isoformat(), "review_timezone": timezone}
    if minutes is not None:
        result["review_minutes"] = minutes
    return result


def label(record: dict) -> str:
    review = instant(record["review_at"]).astimezone(
        ZoneInfo(record.get("review_timezone", DEFAULT_TIMEZONE))
    )
    date = review.strftime("%b %d, %Y").replace(" 0", " ")
    clock = review.strftime("%H:%M:%S" if review.second else "%H:%M")
    return f"{date} at {clock} {review.tzname()}"
