from __future__ import annotations

import math
import os
import threading
from collections import deque
from datetime import UTC, date, datetime, timedelta
from typing import Any

from runner_watch.market_data import (
    EASTERN,
    YahooMarketData,
    YahooQuoteAdapter,
    _last_print,
    session_label,
)
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


HOT_SET_LIMIT = max(1, int(os.getenv("HOT_QUOTE_LIMIT", "30")))


def fresh_quotes(tickers: list[str]) -> dict[str, dict[str, Any]]:

    """The stored quote row for each of these tickers, keyed by ticker."""

    symbols = sorted({str(item).strip().upper() for item in tickers if str(item).strip()})
    if not symbols:
        return {}
    found: dict[str, dict[str, Any]] = {}
    with connection() as database:
        for group in (symbols[index : index + 200] for index in range(0, len(symbols), 200)):
            placeholders = ",".join("?" for _ in group)
            rows = database.execute(
                f"SELECT * FROM ticker_quotes WHERE status='ok' AND ticker IN ({placeholders})",
                group,
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                found[str(row["ticker"])] = row
    return found


def refresh_hot_quotes(tickers: list[str], at: datetime | None = None) -> dict[str, int]:

    """Refresh a small hot set in one batched request rather than one call per ticker.

    The per-ticker lane suits a page view, where one name is wanted right now. Keeping
    the whole board warm that way would spend a request per name every cycle, so the
    hot set rides a single batched download instead.
    """

    symbols = sorted({str(item).strip().upper() for item in tickers if str(item).strip()})[
        :HOT_SET_LIMIT
    ]
    if not symbols:
        return {"requested": 0, "stored": 0, "missing": 0}
    now = _utc(at)
    if not _claim_budget(now):
        return {"requested": len(symbols), "stored": 0, "missing": len(symbols)}
    client = YahooMarketData(
        batch_size=len(symbols), timeout=QUOTE_TIMEOUT_SECONDS, fetch_recorder=record_source_fetch
    )
    try:
        result = client.minutes(symbols)
    except Exception:
        return {"requested": len(symbols), "stored": 0, "missing": len(symbols)}
    stored = 0
    timestamp = now.isoformat()
    session_day = now.astimezone(EASTERN).date()
    with connection() as database:
        for symbol in symbols:
            frame = result.frames.get(symbol)
            print_row = _last_print(frame) if frame is not None else None
            if print_row is None:
                continue
            observed_at, values = print_row
            if not observed_at <= now:
                continue
            previous = _previous_close_for(database, symbol, session_day)
            _save_quote(
                database,
                symbol,
                {
                    "price": values["last"],
                    "observed_at": observed_at.isoformat(),
                    "session": session_label(observed_at.astimezone(EASTERN)),
                    "previous_close": previous,
                    "change_pct": _change_pct(values["last"], previous),
                    "day_high": values["day_high"],
                    "day_low": values["day_low"],
                    "volume": values["volume"],
                    "source": "yahoo",
                    "status": "ok",
                    "last_error": None,
                    "requested_at": timestamp,
                    "collected_at": timestamp,
                },
            )
            stored += 1
    return {
        "requested": len(symbols),
        "stored": stored,
        "missing": len(symbols) - stored,
    }


def _previous_close_for(database: Any, ticker: str, session_day: date) -> float | None:

    """The prior session's close, so a batched mark can carry a move as well as a price.

    A one-minute batch only covers today, so the anchor comes from whatever the rest of
    the system already collected: a previous close the per-ticker lane recorded, or the
    last daily bar before this session.
    """

    row = database.execute(
        "SELECT previous_close FROM ticker_quotes WHERE ticker=?",
        (ticker,),
    ).fetchone()
    stored = _positive_price(row["previous_close"]) if row else None
    if stored is not None:
        return stored
    bar = database.execute(
        """
        SELECT close FROM market_bars
        WHERE ticker=? AND interval='1d' AND bar_time<?
        ORDER BY bar_time DESC LIMIT 1
        """,
        (ticker, session_day.isoformat()),
    ).fetchone()
    return _positive_price(bar["close"]) if bar else None


def _positive_price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return round(price, 4) if math.isfinite(price) and round(price, 4) > 0 else None


def price_marks(
    database: Any,
    ticker: str,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[tuple[datetime, float, str]]:

    """Every stored price observation for a ticker, newest first.

    The on-demand quote lane and the scanner both observe prices on their own cadence.
    Callers that need "the best price we know right now" should read this rather than
    reaching for one lane, so a Call, a saved target and the page all agree.
    """

    symbol = str(ticker).strip().upper()
    if not symbol:
        return []
    marks: list[tuple[datetime, float, str]] = []

    def offer(raw_price: Any, raw_stamp: Any, source: str) -> None:
        price, observed_at = _positive_price(raw_price), _stamp(raw_stamp)
        if price is None or observed_at is None:
            return
        if since is not None and observed_at < since:
            return
        if until is not None and observed_at > until:
            return
        marks.append((observed_at, price, source))

    quote = database.execute(
        "SELECT price,observed_at FROM ticker_quotes WHERE ticker=? AND status='ok'",
        (symbol,),
    ).fetchone()
    if quote:
        offer(quote["price"], quote["observed_at"], "quote")
    snapshot = database.execute(
        """
        SELECT price,quote_time FROM scan_snapshots
        WHERE ticker=? ORDER BY captured_at DESC LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if snapshot:
        offer(snapshot["price"], snapshot["quote_time"], "scan")
    return sorted(marks, reverse=True)


def market_mark(
    ticker: str,
    *,
    at: datetime | None = None,
    refresh: bool = True,
) -> dict[str, Any] | None:

    """The freshest price we know for a ticker, with its age and where it came from."""

    symbol = str(ticker).strip().upper()
    if not symbol:
        return None
    now = _utc(at)
    if refresh:
        try:
            ticker_quote(symbol, at=now)
        except Exception:
            pass
    with connection() as database:
        marks = price_marks(database, symbol, until=now)
    if not marks:
        return None
    observed_at, price, source = marks[0]
    return {
        "ticker": symbol,
        "price": price,
        "observed_at": observed_at.isoformat(),
        "source": source,
        "age_seconds": max(0, int((now - observed_at).total_seconds())),
        "session": session_label(observed_at.astimezone(EASTERN)),
    }


def market_session(at: datetime | None = None) -> str:
    return session_label(_utc(at).astimezone(EASTERN))
