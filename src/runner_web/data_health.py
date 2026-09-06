from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars

from runner_web.db import connection

MARKET_MAX_AGE = timedelta(hours=1)
SPORTS_MAX_AGE = timedelta(minutes=30)
FUTURE_TOLERANCE = timedelta(minutes=5)
EASTERN = ZoneInfo("America/New_York")


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@lru_cache(maxsize=3)
def _calendar(year: int) -> Any:
    return exchange_calendars.get_calendar("XNYS", start=f"{year}-01-01", end=f"{year}-12-31")


def _market_window(at: datetime) -> tuple[datetime, datetime] | None:
    day = at.astimezone(EASTERN).date()
    calendar = _calendar(day.year)
    if not calendar.is_session(day.isoformat()):
        return None
    return (
        calendar.session_open(day.isoformat()).to_pydatetime(),
        calendar.session_close(day.isoformat()).to_pydatetime(),
    )


def stock_session_open_for_day(day_iso: str) -> datetime | None:
    """UTC regular-session open time for a trading day, or None on non-session days."""
    day = date.fromisoformat(day_iso)
    calendar = _calendar(day.year)
    if not calendar.is_session(day_iso):
        return None
    return calendar.session_open(day_iso).to_pydatetime()


def stock_settlement_close(at: datetime) -> datetime:
    """UTC close time of the trading session that settles a stock Call opened at `at`.

    A Call opened before a session's close settles at that same-day close.
    A Call opened after the close, on a weekend, or on a holiday settles at
    the close of the next trading session.
    """
    moment = at.astimezone(UTC)
    probe = at.astimezone(EASTERN).date()
    for _ in range(16):
        calendar = _calendar(probe.year)
        if not calendar.is_session(probe.isoformat()):
            probe += timedelta(days=1)
            continue
        close = calendar.session_close(probe.isoformat()).to_pydatetime()
        if moment < close:
            return close
        probe += timedelta(days=1)
    fallback = datetime.combine(probe, time(16, 0), tzinfo=EASTERN).astimezone(UTC)
    if fallback <= moment:
        fallback += timedelta(days=1)
    return fallback


def quote_health(quote_times: list[Any], at: datetime) -> dict[str, Any]:
    window = _market_window(at)
    if window is None or not window[0] <= at < window[1]:
        return {"status": "idle"}
    timestamps = [_timestamp(value) for value in quote_times]
    valid = [value for value in timestamps if value is not None and value <= at + FUTURE_TOLERANCE]
    latest = max(valid) if valid else None
    age = max(0.0, (at - latest).total_seconds()) if latest else None
    if latest and at - latest <= MARKET_MAX_AGE:
        status = "ok"
    elif at < window[0] + MARKET_MAX_AGE:
        status = "warming"
    else:
        status = "stale" if latest else "missing"
    return {
        "status": status,
        "latest_quote_at": latest.isoformat() if latest else None,
        "age_seconds": round(age) if age is not None else None,
        "maximum_age_seconds": int(MARKET_MAX_AGE.total_seconds()),
    }


def sports_health(run: dict[str, Any] | None, at: datetime) -> dict[str, Any]:
    finished = _timestamp(run.get("finished_at")) if run else None
    age = max(0.0, (at - finished).total_seconds()) if finished else None
    if finished is None:
        status = "missing"
    elif finished > at + FUTURE_TOLERANCE:
        status = "invalid"
    elif run and run.get("status") not in {"success", "partial"}:
        status = "error"
    elif at - finished > SPORTS_MAX_AGE:
        status = "stale"
    else:
        status = "ok"
    return {
        "status": status,
        "last_update_at": finished.isoformat() if finished else None,
        "age_seconds": round(age) if age is not None else None,
        "maximum_age_seconds": int(SPORTS_MAX_AGE.total_seconds()),
    }


def data_health(product: str, *, at: datetime | None = None) -> dict[str, Any]:
    checked_at = at or datetime.now(UTC)
    checked_at = checked_at.replace(tzinfo=UTC) if checked_at.tzinfo is None else checked_at
    try:
        with connection() as database:
            if product == "sports":
                row = database.execute(
                    """
                    SELECT status,finished_at,received_count FROM ingestion_runs
                    WHERE source='espn' AND feed='sports_scoreboard_preview'
                    ORDER BY finished_at DESC LIMIT 1
                    """
                ).fetchone()
                feed = sports_health(dict(row) if row else None, checked_at)
            else:
                rows = database.execute(
                    """
                    SELECT quote_time FROM scan_snapshots WHERE scan_run_id=(
                        SELECT id FROM scan_runs WHERE candidate_rows>0
                        ORDER BY captured_at DESC LIMIT 1
                    )
                    """
                ).fetchall()
                feed = quote_health([row["quote_time"] for row in rows], checked_at)
    except Exception:
        feed = {"status": "unavailable"}
    return {
        "status": "ok" if feed["status"] in {"ok", "idle", "warming"} else "degraded",
        "product": product,
        "checked_at": checked_at.isoformat(),
        "feed": feed,
    }
