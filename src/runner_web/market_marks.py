"""Observed market marks shared by public scoring and feed health."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from runner_web.attention import finite_number


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def usable_market_mark(
    row: dict[str, Any], mark: dict[str, Any] | None, at: datetime
) -> tuple[float, float, datetime] | None:
    """Use a newer observed price and move known by the score time."""
    if not mark or mark.get("status") != "ok":
        return None
    price, change = finite_number(mark.get("price")), finite_number(mark.get("change_pct"))
    observed_at = _timestamp(mark.get("observed_at"))
    collected_at = _timestamp(mark.get("collected_at"))
    scanned_at = _timestamp(row.get("quote_time"))
    if (
        price is None
        or price <= 0
        or change is None
        or observed_at is None
        or collected_at is None
        or observed_at > at
        or collected_at > at
        or observed_at > collected_at
        or (scanned_at is not None and observed_at <= scanned_at)
    ):
        return None
    return price, change, observed_at
