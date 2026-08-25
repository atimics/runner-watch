from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from runner_web.collection import recording_market_data
from runner_web.db import connection

LOG = logging.getLogger(__name__)
HORIZONS = {"1h": timedelta(hours=1), "1d": timedelta(days=1), "5d": timedelta(days=5)}
EASTERN = ZoneInfo("America/New_York")
UPPER_BARRIER_PCT = 8.0
LOWER_BARRIER_PCT = 4.0
BARRIER_HORIZON = timedelta(minutes=60)
BAR_TOLERANCE = timedelta(minutes=10)


def iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def return_pct(base_price: float, later_price: float) -> float | None:
    if not all(math.isfinite(value) and value > 0 for value in (base_price, later_price)):
        return None
    return round((later_price / base_price - 1) * 100, 3)


def due_horizons(row: dict[str, Any], at: datetime | None = None) -> list[str]:
    current = at or datetime.now(UTC)
    base_at = datetime.fromisoformat(str(row["base_at"]))
    if base_at.tzinfo is None:
        base_at = base_at.replace(tzinfo=UTC)
    age = current - base_at.astimezone(UTC)
    return [
        label
        for label, wait in HORIZONS.items()
        if age >= wait and row.get(f"return_{label}_pct") is None
    ]


def _latest_prices(tickers: list[str]) -> dict[str, float]:
    unique = list(dict.fromkeys(tickers))
    if not unique:
        return {}
    result = recording_market_data(batch_size=60).intraday(unique)
    prices: dict[str, float] = {}
    for ticker, frame in result.frames.items():
        close = pd.Series(dtype="float64")
        for column in frame.columns:
            if str(column).lower().replace(" ", "") == "close":
                close = pd.to_numeric(frame[column], errors="coerce").dropna()
                break
        if not close.empty:
            price = float(close.iloc[-1])
            if math.isfinite(price) and price > 0:
                prices[ticker] = price
    return prices


def _state(key: str, value: str, timestamp: str) -> None:
    with connection() as db:
        db.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, value, timestamp),
        )


def refresh_outcomes(at: datetime | None = None) -> dict[str, Any]:
    """Sample later prices so filing scores can be judged against real outcomes."""

    current = at or datetime.now(UTC)
    timestamp = iso(current)
    cutoff = iso(current - timedelta(days=7))
    with connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO sec_outcomes(accession,base_price,base_at,updated_at)
            SELECT accession,price,created_at,? FROM sec_filings
            WHERE price IS NOT NULL AND price>0 AND created_at>=?
            """,
            (timestamp, cutoff),
        )
        rows = db.execute(
            """
            SELECT o.*,f.ticker FROM sec_outcomes o
            JOIN sec_filings f ON f.accession=o.accession
            WHERE o.base_at>=?
            """,
            (cutoff,),
        ).fetchall()

    pending: list[tuple[dict[str, Any], list[str]]] = []
    for raw in rows:
        row = dict(raw)
        horizons = due_horizons(row, current)
        if horizons:
            pending.append((row, horizons))
    prices = _latest_prices([row["ticker"] for row, _ in pending])

    samples_added = 0
    with connection() as db:
        for row, horizons in pending:
            price = prices.get(row["ticker"])
            if price is None:
                continue
            changes: dict[str, Any] = {"updated_at": timestamp}
            for horizon in horizons:
                result = return_pct(float(row["base_price"]), price)
                if result is None:
                    continue
                changes[f"price_{horizon}"] = price
                changes[f"return_{horizon}_pct"] = result
                changes[f"observed_{horizon}_at"] = timestamp
                samples_added += 1
            if len(changes) == 1:
                continue
            assignments = ",".join(f"{column}=?" for column in changes)
            db.execute(
                f"UPDATE sec_outcomes SET {assignments} WHERE accession=?",  # noqa: S608
                (*changes.values(), row["accession"]),
            )
        labeled = int(
            db.execute(
                """
                SELECT COUNT(*) FROM sec_outcomes
                WHERE return_1h_pct IS NOT NULL OR return_1d_pct IS NOT NULL
                      OR return_5d_pct IS NOT NULL
                """
            ).fetchone()[0]
        )

    _state("outcomes_last_refresh", timestamp, timestamp)
    _state("outcomes_labeled_events", str(labeled), timestamp)
    _state("outcomes_last_samples_added", str(samples_added), timestamp)
    return {"events": len(rows), "labeled_events": labeled, "samples_added": samples_added}


def record_outcome_error(exc: Exception) -> None:
    LOG.exception("Outcome sampling failed")
    timestamp = iso()
    _state("outcomes_last_error", str(exc)[:500], timestamp)


Bar = tuple[datetime, float, float, float]


def _bar_prices(tickers: list[str]) -> dict[str, list[Bar]]:
    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    placeholders = ",".join("?" for _ in unique)
    with connection() as db:
        rows = db.execute(
            f"""
            SELECT ticker,bar_time,high,low,close FROM market_bars
            WHERE source='yahoo' AND interval='5m' AND close>0
                  AND ticker IN ({placeholders})
            ORDER BY ticker,bar_time
            """,  # noqa: S608
            unique,
        ).fetchall()
    output: dict[str, list[Bar]] = {}
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row["bar_time"]))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            close = float(row["close"])
            high = float(row["high"]) if row["high"] is not None else close
            low = float(row["low"]) if row["low"] is not None else close
            output.setdefault(str(row["ticker"]), []).append(
                (stamp.astimezone(UTC), high, low, close)
            )
        except (TypeError, ValueError):
            continue
    for bars in output.values():
        bars.sort(key=lambda item: item[0])
    return output


def _first_price_at_or_after(
    bars: list[Bar], target: datetime
) -> tuple[float, datetime] | None:
    target_utc = target.astimezone(UTC)
    return next(((close, stamp) for stamp, _, _, close in bars if stamp >= target_utc), None)


def _price_near_target(bars: list[Bar], target: datetime) -> tuple[float, datetime] | None:
    """Use a nearby bar, but never jump across a closed-session gap."""

    target_utc = target.astimezone(UTC)
    after = next(
        (
            (close, stamp)
            for stamp, _, _, close in bars
            if target_utc <= stamp <= target_utc + BAR_TOLERANCE
        ),
        None,
    )
    if after is not None:
        return after
    before = [
        (close, stamp)
        for stamp, _, _, close in bars
        if target_utc - BAR_TOLERANCE <= stamp < target_utc
    ]
    return before[-1] if before else None


def _scan_horizon_price(
    bars: list[Bar], base_at: datetime, horizon: str
) -> tuple[float, datetime] | None:
    if horizon == "1h":
        return _price_near_target(bars, base_at + timedelta(hours=1))
    base_date = base_at.astimezone(EASTERN).date()
    by_date: dict[Any, list[tuple[datetime, float]]] = {}
    for stamp, _, _, close in bars:
        session_date = stamp.astimezone(EASTERN).date()
        if session_date > base_date:
            by_date.setdefault(session_date, []).append((stamp, close))
    session_dates = sorted(by_date)
    offset = 0 if horizon == "1d" else 4
    if len(session_dates) <= offset:
        return None
    stamp, price = by_date[session_dates[offset]][-1]
    return price, stamp


def barrier_outcome(
    bars: list[Bar], base_at: datetime, base_price: float
) -> dict[str, Any] | None:
    """Label whether +8% or -4% was touched first during the next 60 minutes."""

    base_utc = base_at.astimezone(UTC)
    target = base_utc + BARRIER_HORIZON
    window = [bar for bar in bars if base_utc < bar[0] <= target]
    if not window or not math.isfinite(base_price) or base_price <= 0:
        return None

    upper = base_price * (1 + UPPER_BARRIER_PCT / 100)
    lower = base_price * (1 - LOWER_BARRIER_PCT / 100)
    maximum = max(bar[1] for bar in window)
    minimum = min(bar[2] for bar in window)
    maximum_return = return_pct(base_price, maximum)
    minimum_return = return_pct(base_price, minimum)
    result: dict[str, Any] = {
        "upper_barrier_pct": UPPER_BARRIER_PCT,
        "lower_barrier_pct": LOWER_BARRIER_PCT,
        "horizon_minutes": int(BARRIER_HORIZON.total_seconds() / 60),
        "max_favorable_pct": max(0.0, maximum_return or 0.0),
        "max_adverse_pct": min(0.0, minimum_return or 0.0),
    }
    for stamp, high, low, _ in window:
        touched_up = high >= upper
        touched_down = low <= lower
        if touched_up and touched_down:
            # A 5-minute OHLC bar does not reveal which happened first. Treat
            # it as a loss so training cannot benefit from hidden optimism.
            result.update(
                barrier_label="down",
                barrier_hit_at=iso(stamp),
                barrier_ambiguous=1,
            )
            return result
        if touched_down:
            result.update(
                barrier_label="down",
                barrier_hit_at=iso(stamp),
                barrier_ambiguous=0,
            )
            return result
        if touched_up:
            result.update(
                barrier_label="up",
                barrier_hit_at=iso(stamp),
                barrier_ambiguous=0,
            )
            return result

    # A timeout is valid only when bars cover the start and end of the window.
    # This avoids treating an overnight or halted gap as a calm hour.
    if window[0][0] <= base_utc + BAR_TOLERANCE and window[-1][0] >= target - BAR_TOLERANCE:
        result.update(barrier_label="timeout", barrier_hit_at=None, barrier_ambiguous=0)
        return result
    return None


def refresh_scan_outcomes(at: datetime | None = None) -> dict[str, Any]:
    """Label scan rows from archived bars without looking beyond each horizon."""

    current = at or datetime.now(UTC)
    timestamp = iso(current)
    cutoff = iso(current - timedelta(days=10))
    with connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO scan_outcomes(
                snapshot_id,ticker,base_price,base_at,upper_barrier_pct,
                lower_barrier_pct,horizon_minutes,updated_at
            )
            SELECT id,ticker,price,captured_at,?,?,?,? FROM scan_snapshots
            WHERE scan_run_id IS NOT NULL AND price>0 AND captured_at>=?
            """,
            (UPPER_BARRIER_PCT, LOWER_BARRIER_PCT, 60, timestamp, cutoff),
        )
        db.execute(
            """
            UPDATE scan_outcomes SET base_at=(
                SELECT captured_at FROM scan_snapshots
                WHERE scan_snapshots.id=scan_outcomes.snapshot_id
            )
            WHERE barrier_label IS NULL AND return_1h_pct IS NULL
                  AND return_1d_pct IS NULL AND return_5d_pct IS NULL
            """
        )
        rows = db.execute(
            """
            SELECT * FROM scan_outcomes
            WHERE base_at>=?
            """,
            (cutoff,),
        ).fetchall()

    pending: list[tuple[dict[str, Any], list[str], bool]] = []
    for raw in rows:
        row = dict(raw)
        horizons = due_horizons(row, current)
        base_at = datetime.fromisoformat(str(row["base_at"]))
        if base_at.tzinfo is None:
            base_at = base_at.replace(tzinfo=UTC)
        barrier_due = (
            current.astimezone(UTC) - base_at.astimezone(UTC) >= BARRIER_HORIZON
            and row.get("barrier_label") is None
        )
        if horizons or barrier_due:
            pending.append((row, horizons, barrier_due))

    tickers = [str(row["ticker"]) for row, _, _ in pending]
    # This refresh also archives the newest 5-minute bars through the recorder.
    _latest_prices(tickers)
    prices = _bar_prices(tickers)

    samples_added = 0
    barrier_labels_added = 0
    with connection() as db:
        for row, horizons, barrier_due in pending:
            try:
                base_at = datetime.fromisoformat(str(row["base_at"]))
            except ValueError:
                continue
            if base_at.tzinfo is None:
                base_at = base_at.replace(tzinfo=UTC)
            changes: dict[str, Any] = {"updated_at": timestamp}
            ticker_bars = prices.get(str(row["ticker"]), [])
            if barrier_due:
                barrier = barrier_outcome(ticker_bars, base_at, float(row["base_price"]))
                if barrier is not None:
                    changes.update(barrier)
                    observed_60m = _price_near_target(
                        ticker_bars, base_at + BARRIER_HORIZON
                    )
                    if observed_60m is not None:
                        price_60m, observed_60m_at = observed_60m
                        changes["price_60m"] = price_60m
                        changes["return_60m_pct"] = return_pct(
                            float(row["base_price"]), price_60m
                        )
                        changes["observed_60m_at"] = iso(observed_60m_at)
                    barrier_labels_added += 1
            for horizon in horizons:
                observed = _scan_horizon_price(
                    ticker_bars, base_at, horizon
                )
                if observed is None:
                    continue
                price, observed_at = observed
                result = return_pct(float(row["base_price"]), price)
                if result is None:
                    continue
                changes[f"price_{horizon}"] = price
                changes[f"return_{horizon}_pct"] = result
                changes[f"observed_{horizon}_at"] = iso(observed_at)
                samples_added += 1
            if len(changes) == 1:
                continue
            assignments = ",".join(f"{column}=?" for column in changes)
            db.execute(
                f"UPDATE scan_outcomes SET {assignments} WHERE snapshot_id=?",  # noqa: S608
                (*changes.values(), row["snapshot_id"]),
            )
        labeled = int(
            db.execute(
                """
                SELECT COUNT(*) FROM scan_outcomes
                WHERE return_1h_pct IS NOT NULL OR return_1d_pct IS NOT NULL
                      OR return_5d_pct IS NOT NULL
                """
            ).fetchone()[0]
        )
        barrier_labeled = int(
            db.execute(
                "SELECT COUNT(*) FROM scan_outcomes WHERE barrier_label IS NOT NULL"
            ).fetchone()[0]
        )

    _state("scan_outcomes_last_refresh", timestamp, timestamp)
    _state("scan_outcomes_labeled_rows", str(labeled), timestamp)
    _state("scan_outcomes_barrier_labeled_rows", str(barrier_labeled), timestamp)
    _state("scan_outcomes_last_samples_added", str(samples_added), timestamp)
    return {
        "rows": len(rows),
        "labeled_rows": labeled,
        "barrier_labeled_rows": barrier_labeled,
        "barrier_labels_added": barrier_labels_added,
        "samples_added": samples_added,
    }
