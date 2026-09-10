from __future__ import annotations

import os
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_watch.market_data import EASTERN, YahooQuoteAdapter, session_label
from runner_watch.provider_contracts import DataKind, ProviderRequest
from runner_watch.provider_registry import ProviderRegistry
from runner_web.db import connection
from runner_web.ingestion import record_source_fetch

QUOTE_TTL_SECONDS = max(5, int(os.getenv("TICKER_QUOTE_TTL_SECONDS", "30")))
QUOTE_CALLS_PER_MINUTE = max(1, int(os.getenv("TICKER_QUOTE_CALLS_PER_MINUTE", "40")))
QUOTE_TIMEOUT_SECONDS = max(2.0, float(os.getenv("TICKER_QUOTE_TIMEOUT_SECONDS", "8")))
QUOTE_MAX_AGE = timedelta(minutes=int(os.getenv("TICKER_QUOTE_MAX_AGE_MINUTES", "30")))

_BUDGET_LOCK = threading.Lock()
_BUDGET_CALLS: deque[datetime] = deque()
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def _stamp(value: Any) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _claim_budget(now: datetime) -> bool:

    window = now - timedelta(seconds=60)
    with _BUDGET_LOCK:
        while _BUDGET_CALLS and _BUDGET_CALLS[0] <= window:
            _BUDGET_CALLS.popleft()
        if len(_BUDGET_CALLS) >= QUOTE_CALLS_PER_MINUTE:
            return False
        _BUDGET_CALLS.append(now)
        return True


def _quote_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        YahooQuoteAdapter(fetch_recorder=record_source_fetch, timeout=QUOTE_TIMEOUT_SECONDS)
    )
    registry.route(DataKind.QUOTES, "yahoo")
    return registry


def _stored_quote(database: Any, ticker: str) -> dict[str, Any] | None:
    row = database.execute("SELECT * FROM ticker_quotes WHERE ticker=?", (ticker,)).fetchone()
    return dict(row) if row else None


def _save_quote(database: Any, ticker: str, values: dict[str, Any]) -> None:
    columns = (
        "price",
        "observed_at",
        "session",
        "previous_close",
        "change_pct",
        "day_high",
        "day_low",
        "volume",
        "source",
        "status",
        "last_error",
        "requested_at",
        "collected_at",
    )
    assignments = ",".join(f"{name}=excluded.{name}" for name in columns)
    database.execute(
        f"""
        INSERT INTO ticker_quotes(ticker,{",".join(columns)})
        VALUES({",".join("?" for _ in range(len(columns) + 1))})
        ON CONFLICT(ticker) DO UPDATE SET {assignments}
        """,
        (ticker, *(values.get(name) for name in columns)),
    )


def _change_pct(price: Any, previous_close: Any) -> float | None:
    try:
        last, base = float(price), float(previous_close)
    except (TypeError, ValueError):
        return None
    if base <= 0:
        return None
    return round((last / base - 1) * 100, 2)


def _public_quote(row: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:

    if not row:
        return None
    observed_at = _stamp(row.get("observed_at"))
    age = (now - observed_at).total_seconds() if observed_at else None
    return {
        "ticker": str(row["ticker"]),
        "price": row.get("price"),
        "observed_at": row.get("observed_at"),
        "session": row.get("session"),
        "previous_close": row.get("previous_close"),
        "change_pct": row.get("change_pct"),
        "day_high": row.get("day_high"),
        "day_low": row.get("day_low"),
        "volume": row.get("volume"),
        "source": row.get("source"),
        "status": row.get("status"),
        "collected_at": row.get("collected_at"),
        "age_seconds": int(age) if age is not None else None,
        "fresh": bool(age is not None and age <= QUOTE_MAX_AGE.total_seconds()),
    }


def _fetch_quote(ticker: str, now: datetime) -> dict[str, Any]:

    values: dict[str, Any] = {
        "source": "yahoo",
        "requested_at": now.isoformat(),
        "status": "error",
        "last_error": None,
    }
    try:
        batch = _quote_registry().fetch(
            ProviderRequest(kind=DataKind.QUOTES, symbols=(ticker,))
        )
        quote = next((item for item in batch.quotes if item.symbol == ticker), None)
    except Exception as exc:
        values["last_error"] = type(exc).__name__
        return values
    if quote is None:
        values.update(status="empty", last_error="no_quote")
        return values
    values.update(
        price=quote.last,
        observed_at=quote.observed_at.isoformat(),
        session=quote.session,
        previous_close=quote.previous_close,
        change_pct=_change_pct(quote.last, quote.previous_close),
        day_high=quote.day_high,
        day_low=quote.day_low,
        volume=quote.volume,
        status="ok",
        collected_at=batch.provenance.collected_at.isoformat(),
    )
    return values


def ticker_quote(
    ticker: str,
    *,
    at: datetime | None = None,
    refresh: bool = True,
    ttl_seconds: int | None = None,
) -> dict[str, Any] | None:

    symbol = str(ticker).strip().upper()
    if not symbol:
        return None
    now = _utc(at)
    ttl = QUOTE_TTL_SECONDS if ttl_seconds is None else max(0, ttl_seconds)
    with connection() as database:
        stored = _stored_quote(database, symbol)
    requested_at = _stamp(stored.get("requested_at")) if stored else None
    if stored and requested_at and (now - requested_at).total_seconds() < ttl:
        return _public_quote(stored, now)
    if not refresh or not _claim_budget(now):
        return _public_quote(stored, now)
    with _INFLIGHT_LOCK:
        if symbol in _INFLIGHT:
            return _public_quote(stored, now)
        _INFLIGHT.add(symbol)
    try:
        values = _fetch_quote(symbol, now)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(symbol)
    if values["status"] != "ok" and stored and stored.get("status") == "ok":
        with connection() as database:
            database.execute(
                "UPDATE ticker_quotes SET requested_at=?,last_error=? WHERE ticker=?",
                (now.isoformat(), values.get("last_error"), symbol),
            )
        refreshed = {**stored, "requested_at": now.isoformat()}
        return _public_quote(refreshed, now)
    with connection() as database:
        _save_quote(database, symbol, values)
        saved = _stored_quote(database, symbol)
    return _public_quote(saved, now)


def market_session(at: datetime | None = None) -> str:
    return session_label(_utc(at).astimezone(EASTERN))
