from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
PRE_MARKET = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
AFTER_HOURS_CLOSE = time(20, 0)


def _at(day: datetime, value: time) -> datetime:
    return datetime.combine(day.date(), value, tzinfo=EASTERN)


def _next_sunday_evening(current: datetime) -> datetime:
    days = (6 - current.weekday()) % 7
    target = _at(current + timedelta(days=days), AFTER_HOURS_CLOSE)
    if target <= current:
        target += timedelta(days=7)
    return target


def market_clock(moment: datetime | None = None) -> dict[str, Any]:
    current = (moment or datetime.now(UTC)).astimezone(EASTERN)
    weekday = current.weekday()
    local_time = current.time().replace(tzinfo=None)

    if weekday == 6 and local_time >= AFTER_HOURS_CLOSE:
        session = "overnight"
        label = "Overnight"
        next_at = _at(current + timedelta(days=1), PRE_MARKET)
        next_label = "Pre-market"
    elif weekday == 6:
        session = "closed"
        label = "Weekend closed"
        next_at = _at(current, AFTER_HOURS_CLOSE)
        next_label = "Overnight venues"
    elif weekday == 5:
        session = "closed"
        label = "Weekend closed"
        next_at = _next_sunday_evening(current)
        next_label = "Overnight venues"
    elif local_time < PRE_MARKET:
        session = "overnight"
        label = "Overnight"
        next_at = _at(current, PRE_MARKET)
        next_label = "Pre-market"
    elif local_time < REGULAR_OPEN:
        session = "pre"
        label = "Pre-market"
        next_at = _at(current, REGULAR_OPEN)
        next_label = "Regular open"
    elif local_time < REGULAR_CLOSE:
        session = "regular"
        label = "Regular hours"
        next_at = _at(current, REGULAR_CLOSE)
        next_label = "After-hours"
    elif local_time < AFTER_HOURS_CLOSE:
        session = "after"
        label = "After-hours"
        next_at = _at(current, AFTER_HOURS_CLOSE)
        next_label = "Overnight" if weekday < 4 else "Weekend close"
    elif weekday < 4:
        session = "overnight"
        label = "Overnight"
        next_at = _at(current + timedelta(days=1), PRE_MARKET)
        next_label = "Pre-market"
    else:
        session = "closed"
        label = "Weekend closed"
        next_at = _next_sunday_evening(current)
        next_label = "Overnight venues"

    scanner_active = session in {"pre", "regular", "after"}
    return {
        "server_now": current.astimezone(UTC).isoformat(),
        "eastern_now": current.isoformat(),
        "session": session,
        "label": label,
        "next_at": next_at.astimezone(UTC).isoformat(),
        "next_label": next_label,
        "countdown_seconds": max(0, int((next_at - current).total_seconds())),
        "scanner_active": scanner_active,
        "data_note": (
            "Scanner collecting delayed extended-hours data"
            if scanner_active
            else "Overnight access is broker-dependent; scanner resumes at 4:00 ET"
        ),
        "hours": [
            {"key": "overnight", "short": "OVN", "range": "8p–4a"},
            {"key": "pre", "short": "PRE", "range": "4–9:30a"},
            {"key": "regular", "short": "REG", "range": "9:30a–4p"},
            {"key": "after", "short": "AH", "range": "4–8p"},
        ],
        "schedule_note": "US Eastern · holidays and early closes may differ",
    }
