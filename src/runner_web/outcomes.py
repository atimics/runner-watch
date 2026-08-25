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


def _bar_prices(tickers: list[str]) -> dict[str, list[tuple[datetime, float]]]:
    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    placeholders = ",".join("?" for _ in unique)
    with connection() as db:
        rows = db.execute(
            f"""
            SELECT ticker,bar_time,close FROM market_bars
            WHERE source='yahoo' AND interval='5m' AND close>0
                  AND ticker IN ({placeholders})
            ORDER BY ticker,bar_time
            """,  # noqa: S608
            unique,
        ).fetchall()
    output: dict[str, list[tuple[datetime, float]]] = {}
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row["bar_time"]))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            output.setdefault(str(row["ticker"]), []).append(
                (stamp.astimezone(UTC), float(row["close"]))
            )
        except (TypeError, ValueError):
            continue
    for bars in output.values():
        bars.sort(key=lambda item: item[0])
    return output


def _first_price_at_or_after(
    bars: list[tuple[datetime, float]], target: datetime
) -> tuple[float, datetime] | None:
    target_utc = target.astimezone(UTC)
    return next(((price, stamp) for stamp, price in bars if stamp >= target_utc), None)


def _scan_horizon_price(
    bars: list[tuple[datetime, float]], base_at: datetime, horizon: str
) -> tuple[float, datetime] | None:
    if horizon == "1h":
        return _first_price_at_or_after(bars, base_at + timedelta(hours=1))
    base_date = base_at.astimezone(EASTERN).date()
    by_date: dict[Any, list[tuple[datetime, float]]] = {}
    for stamp, price in bars:
        session_date = stamp.astimezone(EASTERN).date()
        if session_date > base_date:
            by_date.setdefault(session_date, []).append((stamp, price))
    session_dates = sorted(by_date)
    offset = 0 if horizon == "1d" else 4
    if len(session_dates) <= offset:
        return None
    stamp, price = by_date[session_dates[offset]][-1]
    return price, stamp


def refresh_scan_outcomes(at: datetime | None = None) -> dict[str, Any]:
    """Label complete scan groups from the first stored bar after each horizon."""

    current = at or datetime.now(UTC)
    timestamp = iso(current)
    cutoff = iso(current - timedelta(days=10))
    with connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO scan_outcomes(
                snapshot_id,ticker,base_price,base_at,updated_at
            )
            SELECT id,ticker,price,quote_time,? FROM scan_snapshots
            WHERE scan_run_id IS NOT NULL AND price>0 AND captured_at>=?
            """,
            (timestamp, cutoff),
        )
        rows = db.execute(
            """
            SELECT * FROM scan_outcomes
            WHERE base_at>=?
            """,
            (cutoff,),
        ).fetchall()

    pending: list[tuple[dict[str, Any], list[str]]] = []
    for raw in rows:
        row = dict(raw)
        horizons = due_horizons(row, current)
        if horizons:
            pending.append((row, horizons))

    tickers = [str(row["ticker"]) for row, _ in pending]
    # This refresh also archives the newest 5-minute bars through the recorder.
    _latest_prices(tickers)
    prices = _bar_prices(tickers)

    samples_added = 0
    with connection() as db:
        for row, horizons in pending:
            try:
                base_at = datetime.fromisoformat(str(row["base_at"]))
            except ValueError:
                continue
            if base_at.tzinfo is None:
                base_at = base_at.replace(tzinfo=UTC)
            changes: dict[str, Any] = {"updated_at": timestamp}
            for horizon in horizons:
                observed = _scan_horizon_price(
                    prices.get(str(row["ticker"]), []), base_at, horizon
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

    _state("scan_outcomes_last_refresh", timestamp, timestamp)
    _state("scan_outcomes_labeled_rows", str(labeled), timestamp)
    _state("scan_outcomes_last_samples_added", str(samples_added), timestamp)
    return {"rows": len(rows), "labeled_rows": labeled, "samples_added": samples_added}
