from __future__ import annotations

import gzip
import hashlib
import json
import math
import uuid
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from runner_watch.ingestion import SourceFetch
from runner_web.db import connection

TERMINAL_ITEM_STATUSES = {"processed", "ignored", "rejected"}


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


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


def _requested_count(fetch: SourceFetch) -> int:
    requested = fetch.metadata.get("requested_tickers")
    if isinstance(requested, list):
        return len(requested)
    if fetch.source == "sec":
        return 1
    size = fetch.metadata.get("size")
    return int(size) if isinstance(size, int) and size >= 0 else 0


def _received_count(fetch: SourceFetch) -> int:
    if fetch.status == "error":
        return 0
    if fetch.feed == "market_bars" and isinstance(fetch.payload, dict):
        return len(fetch.payload)
    if fetch.feed == "universe" and isinstance(fetch.payload, list):
        return len(fetch.payload)
    return 1 if fetch.payload is not None else 0


def _archive_document(database: Any, fetch: SourceFetch, digest: str, collected_at: str) -> None:
    body = fetch.payload
    if not isinstance(body, bytes):
        return
    if len(body) > 512:
        stored_body = gzip.compress(body, compresslevel=6, mtime=0)
        content_encoding = "gzip"
    else:
        stored_body = body
        content_encoding = "identity"
    database.execute(
        """
        INSERT INTO source_documents(
            source,source_url,content_hash,content_type,content_encoding,content,
            first_collected_at,last_collected_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(source_url,content_hash) DO UPDATE SET
            last_collected_at=excluded.last_collected_at
        """,
        (
            fetch.source,
            fetch.locator,
            digest,
            fetch.content_type or "application/octet-stream",
            content_encoding,
            stored_body,
            collected_at,
            collected_at,
        ),
    )


def _market_projection(
    database: Any, run_id: str, fetch: SourceFetch, collected_at: str
) -> str:
    frames = fetch.payload
    if not isinstance(frames, dict):
        raise TypeError("Yahoo market payload must be a ticker-to-frame mapping")
    interval = str(fetch.metadata.get("interval") or "unknown")
    rows: list[tuple[Any, ...]] = []
    digest = hashlib.sha256()
    found: set[str] = set()
    for ticker, frame in sorted(frames.items()):
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        symbol = str(ticker).upper()
        found.add(symbol)
        columns = {
            name: _column(frame, name) for name in ("Open", "High", "Low", "Close", "Volume")
        }
        first_bar: str | None = None
        last_bar: str | None = None
        bar_count = 0
        for index in frame.index:
            bar_time = pd.Timestamp(index).isoformat()
            values = (
                "yahoo",
                symbol,
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
            rows.append(values)
            digest.update(_json(values[:9]).encode())
            first_bar = first_bar or bar_time
            last_bar = bar_time
            bar_count += 1
        database.execute(
            """
            INSERT INTO ingestion_items(run_id,item_key,status,payload_json)
            VALUES(?,?,?,?)
            """,
            (
                run_id,
                symbol,
                "accepted",
                _json(
                    {
                        "interval": interval,
                        "bar_count": bar_count,
                        "first_bar": first_bar,
                        "last_bar": last_bar,
                    }
                ),
            ),
        )
    for ticker in fetch.metadata.get("missing_tickers", []):
        symbol = str(ticker).upper()
        if symbol in found:
            continue
        database.execute(
            """
            INSERT INTO ingestion_items(run_id,item_key,status,payload_json,error)
            VALUES(?,?,?,?,?)
            """,
            (run_id, symbol, "missing", "{}", "Yahoo returned no usable frame"),
        )
    if rows:
        database.executemany(
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
    return digest.hexdigest()


def _universe_projection(database: Any, run_id: str, fetch: SourceFetch) -> str:
    quotes = fetch.payload
    if not isinstance(quotes, list):
        raise TypeError("Yahoo universe payload must be a list")
    digest = hashlib.sha256(_json(quotes).encode()).hexdigest()
    for index, quote in enumerate(quotes):
        payload = quote if isinstance(quote, dict) else {"value": quote}
        symbol = str(payload.get("symbol") or f"row-{index}").strip().upper()
        database.execute(
            """
            INSERT OR REPLACE INTO ingestion_items(
                run_id,item_key,status,payload_json,error
            ) VALUES(?,?,?,?,?)
            """,
            (
                run_id,
                symbol,
                "accepted" if payload.get("symbol") else "rejected",
                _json(payload),
                None if payload.get("symbol") else "Universe row has no symbol",
            ),
        )
    return digest


def record_source_fetch(fetch: SourceFetch) -> str:
    """Send SEC and Yahoo fetches through the same durable ingestion pipe."""

    run_id = uuid.uuid4().hex
    metadata_json = _json(fetch.metadata)
    requested_count = _requested_count(fetch)
    received_count = _received_count(fetch)
    with connection() as database:
        database.execute(
            """
            INSERT INTO ingestion_runs(
                id,source,feed,locator,status,requested_count,received_count,
                content_hash,content_type,metadata_json,error,started_at,finished_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                fetch.source,
                fetch.feed,
                fetch.locator,
                "recording" if fetch.status != "error" else "error",
                requested_count,
                received_count,
                None,
                fetch.content_type,
                metadata_json,
                fetch.error,
                _iso(fetch.started_at),
                _iso(fetch.finished_at),
            ),
        )
    if fetch.status == "error":
        return run_id

    try:
        collected_at = _iso(fetch.finished_at)
        with connection() as database:
            if isinstance(fetch.payload, bytes):
                content_hash = hashlib.sha256(fetch.payload).hexdigest()
                _archive_document(database, fetch, content_hash, collected_at)
            elif fetch.source == "yahoo" and fetch.feed == "market_bars":
                content_hash = _market_projection(database, run_id, fetch, collected_at)
            elif fetch.source == "yahoo" and fetch.feed == "universe":
                content_hash = _universe_projection(database, run_id, fetch)
            else:
                content_hash = hashlib.sha256(_json(fetch.payload).encode()).hexdigest()
            database.execute(
                """
                UPDATE ingestion_runs SET status=?,content_hash=?,error=NULL WHERE id=?
                """,
                (fetch.status, content_hash, run_id),
            )
    except Exception as exc:
        with connection() as database:
            database.execute(
                """
                UPDATE ingestion_runs SET status='error',error=? WHERE id=?
                """,
                (f"Projection failed: {exc}"[:1000], run_id),
            )
        raise
    return run_id


def mark_source_item(
    *,
    source: str,
    feed: str,
    item_key: str,
    status: str,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
    parser_version: str | None = None,
) -> None:
    timestamp = _iso()
    processed_at = timestamp if status in TERMINAL_ITEM_STATUSES else None
    with connection() as database:
        database.execute(
            """
            INSERT INTO source_item_state(
                source,feed,item_key,status,payload_json,error,parser_version,
                attempt_count,first_seen_at,last_seen_at,processed_at
            ) VALUES(?,?,?,?,?,?,?,1,?,?,?)
            ON CONFLICT(source,feed,item_key) DO UPDATE SET
                status=excluded.status,payload_json=excluded.payload_json,
                error=excluded.error,parser_version=excluded.parser_version,
                attempt_count=source_item_state.attempt_count+1,
                last_seen_at=excluded.last_seen_at,processed_at=excluded.processed_at
            """,
            (
                source,
                feed,
                item_key,
                status,
                _json(payload or {}),
                error[:1000] if error else None,
                parser_version,
                timestamp,
                timestamp,
                processed_at,
            ),
        )


def source_item_is_terminal(source: str, feed: str, item_key: str) -> bool:
    with connection() as database:
        row = database.execute(
            """
            SELECT status FROM source_item_state
            WHERE source=? AND feed=? AND item_key=?
            """,
            (source, feed, item_key),
        ).fetchone()
    return bool(row and row["status"] in TERMINAL_ITEM_STATUSES)


def ingestion_status() -> dict[str, Any]:
    """Return a small operational view without exposing stored source payloads."""

    with connection() as database:
        run_rows = database.execute(
            """
            SELECT source,feed,COUNT(*) AS runs,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successful,
                   SUM(CASE WHEN status='partial' THEN 1 ELSE 0 END) AS partial,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed,
                   MAX(CASE WHEN status IN ('success','partial') THEN finished_at END)
                       AS last_success_at,
                   MAX(CASE WHEN status='error' THEN finished_at END) AS last_error_at
            FROM ingestion_runs GROUP BY source,feed ORDER BY source,feed
            """
        ).fetchall()
        item_rows = database.execute(
            """
            SELECT source,feed,status,COUNT(*) AS items
            FROM source_item_state GROUP BY source,feed,status
            ORDER BY source,feed,status
            """
        ).fetchall()
    return {
        "feeds": [dict(row) for row in run_rows],
        "item_states": [dict(row) for row in item_rows],
    }
