from __future__ import annotations

import gzip
import hashlib
import math
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from runner_watch.market_data import YahooMarketData
from runner_web.db import connection


def _iso() -> str:
    return datetime.now(UTC).isoformat()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _column(frame: pd.DataFrame, name: str) -> pd.Series:
    for column in frame.columns:
        if str(column).lower().replace(" ", "") == name.lower():
            return frame[column]
    return pd.Series(index=frame.index, dtype="float64")


def record_market_bars(interval: str, frames: dict[str, pd.DataFrame]) -> None:
    """Keep the Yahoo bars that were already downloaded by a collector.

    Yahoo can revise an incomplete bar. The first and last collection times are
    retained while the values are updated to the newest observed version.
    """

    collected_at = _iso()
    rows: list[tuple[Any, ...]] = []
    for ticker, frame in frames.items():
        if frame.empty:
            continue
        columns = {
            name: _column(frame, name)
            for name in ("Open", "High", "Low", "Close", "Volume")
        }
        for index in frame.index:
            stamp = pd.Timestamp(index)
            bar_time = stamp.isoformat()
            rows.append(
                (
                    "yahoo",
                    ticker.upper(),
                    interval,
                    bar_time,
                    _number(columns["Open"].get(index)),
                    _number(columns["High"].get(index)),
                    _number(columns["Low"].get(index)),
                    _number(columns["Close"].get(index)),
                    _number(columns["Volume"].get(index)),
                    collected_at,
                    collected_at,
                )
            )
    if not rows:
        return
    with connection() as db:
        db.executemany(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,open,high,low,close,volume,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source,ticker,interval,bar_time) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,
                close=excluded.close,volume=excluded.volume,
                last_collected_at=excluded.last_collected_at
            """,
            rows,
        )


def record_source_document(url: str, body: bytes) -> None:
    """Store every distinct SEC response body fetched by the app."""

    collected_at = _iso()
    digest = hashlib.sha256(body).hexdigest()
    stripped = body.lstrip()
    if stripped.startswith((b"{", b"[")):
        content_type = "application/json"
    elif stripped.startswith(b"<"):
        content_type = "application/xml"
    else:
        content_type = "application/octet-stream"
    if len(body) > 512:
        stored_body = gzip.compress(body, compresslevel=6, mtime=0)
        content_encoding = "gzip"
    else:
        stored_body = body
        content_encoding = "identity"
    with connection() as db:
        db.execute(
            """
            INSERT INTO source_documents(
                source,source_url,content_hash,content_type,content_encoding,content,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(source_url,content_hash) DO UPDATE SET
                last_collected_at=excluded.last_collected_at
            """,
            (
                "sec",
                url,
                digest,
                content_type,
                content_encoding,
                stored_body,
                collected_at,
                collected_at,
            ),
        )


def recording_market_data(batch_size: int = 60, timeout: float = 15.0) -> YahooMarketData:
    return YahooMarketData(
        batch_size=batch_size,
        timeout=timeout,
        recorder=record_market_bars,
    )
