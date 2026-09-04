from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from runner_web.db import connection
from runner_web.product_policy import BASE_RATES

EASTERN = ZoneInfo("America/New_York")
MIN_BASE_RATE_SAMPLES = BASE_RATES.minimum_samples
BASE_RATE_LOOKBACK_DAYS = BASE_RATES.lookback_days
MATCH_TOLERANCE_MINUTES = BASE_RATES.clock_tolerance_minutes

MARKET_METRICS = {
    "relative_volume": "Relative volume",
    "recent_relative_volume": "Recent volume",
    "momentum_15m_pct": "15m momentum",
    "momentum_acceleration_pct": "Momentum acceleration",
    "vwap_position_pct": "VWAP position",
    "breakout_pct": "Breakout",
}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _quantile(sorted_values: list[float], fraction: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def empirical_receipt(
    observed: Any,
    controls: Iterable[Any],
    *,
    label: str,
    minimum_samples: int = MIN_BASE_RATE_SAMPLES,
) -> dict[str, Any]:

    observed_number = _number(observed)
    values = sorted(value for item in controls if (value := _number(item)) is not None)
    base = {
        "label": label,
        "observed": observed_number,
        "sample_count": len(values),
        "minimum_samples": minimum_samples,
        "mode": "insufficient_data",
        "expected": None,
        "interquartile_range": None,
        "percentile": None,
        "multiple_of_expected": None,
        "notable": False,
    }
    if observed_number is None:
        return {**base, "insufficient_reason": "current value is unavailable"}
    if not values:
        return {**base, "insufficient_reason": "no matched historical observations"}

    median = float(statistics.median(values))
    base.update(
        {
            "expected": round(median, 4),
            "interquartile_range": [
                round(_quantile(values, 0.25), 4),
                round(_quantile(values, 0.75), 4),
            ],
            "multiple_of_expected": (round(observed_number / median, 3) if median > 0 else None),
        }
    )
    if len(values) < minimum_samples:
        return {
            **base,
            "insufficient_reason": (
                f"need {minimum_samples} matched sessions, found {len(values)}"
            ),
        }

    below = sum(value < observed_number for value in values)
    equal = sum(value == observed_number for value in values)
    percentile = (below + 0.5 * equal) / len(values)
    return {
        **base,
        "mode": "empirical",
        "percentile": round(percentile, 4),
        "notable": percentile >= 0.9,
        "insufficient_reason": None,
    }


def matched_market_base_rates(current: dict[str, Any]) -> dict[str, Any]:

    ticker = str(current.get("ticker") or "").strip().upper()
    observed_at = _time(current.get("captured_at") or current.get("event_at"))
    session = str(current.get("session") or "").strip().lower()
    empty = {
        "method": "same_ticker_session_clock",
        "method_version": 1,
        "ticker": ticker or None,
        "as_of": observed_at.isoformat() if observed_at else None,
        "session": session or None,
        "mode": "insufficient_data",
        "matched_sessions": 0,
        "minimum_samples": MIN_BASE_RATE_SAMPLES,
        "lookback_days": BASE_RATE_LOOKBACK_DAYS,
        "clock_tolerance_minutes": MATCH_TOLERANCE_MINUTES,
        "metrics": {},
        "notable_metrics": [],
    }
    if not ticker or observed_at is None:
        return {**empty, "insufficient_reason": "ticker timestamp is unavailable"}

    with connection() as database:
        rows = database.execute(
            """
            SELECT ticker,session,captured_at,relative_volume,recent_relative_volume,
                   momentum_15m_pct,momentum_acceleration_pct,vwap_position_pct,breakout_pct
            FROM scan_snapshots
            WHERE ticker=? AND captured_at>=? AND captured_at<?
            ORDER BY captured_at DESC
            """,
            (
                ticker,
                (observed_at - timedelta(days=BASE_RATE_LOOKBACK_DAYS)).isoformat(),
                observed_at.isoformat(),
            ),
        ).fetchall()

    observed_local = observed_at.astimezone(EASTERN)
    observed_minute = observed_local.hour * 60 + observed_local.minute
    matched_by_date: dict[Any, tuple[int, dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        row_at = _time(row.get("captured_at"))
        if row_at is None:
            continue
        local = row_at.astimezone(EASTERN)
        if local.date() == observed_local.date():
            continue
        if session and str(row.get("session") or "").lower() != session:
            continue
        distance = abs((local.hour * 60 + local.minute) - observed_minute)
        if distance > MATCH_TOLERANCE_MINUTES:
            continue
        existing = matched_by_date.get(local.date())
        if existing is None or distance < existing[0]:
            matched_by_date[local.date()] = (distance, row)

    matched_rows = [match[1] for match in matched_by_date.values()]
    metrics = {
        key: empirical_receipt(
            current.get(key),
            (row.get(key) for row in matched_rows),
            label=label,
        )
        for key, label in MARKET_METRICS.items()
        if current.get(key) is not None
    }
    empirical = [receipt for receipt in metrics.values() if receipt["mode"] == "empirical"]
    notable = [key for key, receipt in metrics.items() if receipt["notable"]]
    return {
        **empty,
        "mode": "empirical" if empirical else "insufficient_data",
        "matched_sessions": len(matched_rows),
        "metrics": metrics,
        "notable_metrics": notable,
        "insufficient_reason": (
            None
            if empirical
            else f"need {MIN_BASE_RATE_SAMPLES} matched sessions, found {len(matched_rows)}"
        ),
    }
