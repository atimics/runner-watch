from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from runner_watch.market_data import YahooMarketData
from runner_web.db import connection

LOG = logging.getLogger(__name__)
HORIZONS = {"1h": timedelta(hours=1), "1d": timedelta(days=1), "5d": timedelta(days=5)}


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
    result = YahooMarketData(batch_size=60).intraday(unique)
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
