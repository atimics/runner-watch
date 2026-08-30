"""Massive daily bar provider.

Massive (https://massive.com) exposes a Polygon-shaped REST API. The grouped
daily endpoint returns one day of OHLCV bars for every listed US stock in a
single request, so one call per trading session covers the whole scanner
universe. The Bars Basic plan is end-of-day with about 5 calls per minute, so
this module keeps a local SQLite cache of grouped sessions and bounds how many
uncached sessions a scan-time fetch may backfill before it fails and lets the
provider registry fall back to Yahoo.

Environment:

- MASSIVE_API_KEY: enables the provider when present.
- MASSIVE_ENABLED: set to a false value to disable without removing the key.
- MASSIVE_CALLS_PER_MINUTE: token bucket size/refill, default 5 (Basic plan).
- MASSIVE_MAX_SCAN_CALLS: uncached-session budget per scan-time fetch, default 10.
- MASSIVE_CACHE_PATH: SQLite cache location, default data/massive_daily.sqlite3.
- MASSIVE_TIMEOUT_SECONDS: per-request HTTP timeout, default 15.
- MASSIVE_BACKFILL_CALLS: uncached-session budget for each worker warm-up pass,
  default 20. A warm cache makes no calls.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from runner_watch.ingestion import SourceFetch, SourceFetchRecorder
from runner_watch.provider_contracts import (
    Bar,
    DataKind,
    FetchBatch,
    ProviderProvenance,
    ProviderRequest,
)

ProgressCallback = Callable[[int, int, str], None]
EASTERN = ZoneInfo("America/New_York")

MASSIVE_API_ROOT = "https://api.massive.com"
GROUPED_DAILY_PATH = "/v2/aggs/grouped/locale/us/market/stocks/{date}"
DEFAULT_CACHE_PATH = "data/massive_daily.sqlite3"
USER_AGENT = "RunnerWatch/0.3 https://stonks.rati.foundation"
MAX_WAIT_PER_CALL_SECONDS = 30.0

_PERIOD_DAYS = {
    "5d": 7,
    "1mo": 31,
    "3mo": 92,
    "6mo": 183,
    "1y": 365,
    "2y": 730,
}


class MassiveAPIError(RuntimeError):
    """Raised when Massive cannot serve a request cleanly."""


class MassiveRateLimitError(MassiveAPIError):
    """Raised when honoring the rate limit would block the caller too long."""


def massive_api_key() -> str:
    return os.getenv("MASSIVE_API_KEY", "").strip()


def massive_enabled(api_key: str | None = None) -> bool:
    if not (api_key or massive_api_key()):
        return False
    flag = os.getenv("MASSIVE_ENABLED", "true").strip().lower()
    return flag not in {"0", "false", "no", "off"}


def massive_calls_per_minute() -> int:
    try:
        return max(1, int(os.getenv("MASSIVE_CALLS_PER_MINUTE", "5").strip() or "5"))
    except ValueError:
        return 5


def massive_max_scan_calls() -> int:
    try:
        return max(0, int(os.getenv("MASSIVE_MAX_SCAN_CALLS", "10").strip() or "10"))
    except ValueError:
        return 10


def massive_cache_path() -> Path:
    return Path(os.getenv("MASSIVE_CACHE_PATH", DEFAULT_CACHE_PATH).strip() or DEFAULT_CACHE_PATH)


def massive_timeout_seconds() -> float:
    try:
        return max(1.0, float(os.getenv("MASSIVE_TIMEOUT_SECONDS", "15").strip() or "15"))
    except ValueError:
        return 15.0


def massive_backfill_calls() -> int:
    """Uncached-session budget for one worker warm-up pass."""
    try:
        return max(0, int(os.getenv("MASSIVE_BACKFILL_CALLS", "20").strip() or "20"))
    except ValueError:
        return 20


def _weekdays(start: date, end: date) -> list[date]:
    """Inclusive list of weekday dates between start and end."""
    output: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            output.append(current)
        current += timedelta(days=1)
    return output


def _session_dates(period: str | None, today_et: date) -> list[date]:
    """Completed weekday sessions covered by a yfinance-style period string."""
    days = _PERIOD_DAYS.get((period or "1y").strip(), 365)
    start = today_et - timedelta(days=days)
    return _weekdays(start, today_et - timedelta(days=1))


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class RateLimiter:
    """Thread-safe token bucket sized for the Massive plan's call rate."""

    def __init__(self, calls_per_minute: int = 5) -> None:
        if calls_per_minute < 1:
            raise ValueError("calls_per_minute must be at least 1")
        self._capacity = float(calls_per_minute)
        self._rate = calls_per_minute / 60.0
        self._tokens = self._capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, max_wait_seconds: float = MAX_WAIT_PER_CALL_SECONDS) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            if wait > max_wait_seconds:
                raise MassiveRateLimitError(f"Massive rate limit would need {wait:.1f}s of waiting")
            time.sleep(wait + 0.05)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_bars (
    session_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    vwap REAL,
    transactions INTEGER,
    PRIMARY KEY (session_date, symbol)
);
CREATE TABLE IF NOT EXISTS fetched_dates (
    session_date TEXT PRIMARY KEY,
    rows INTEGER NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_limit (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_call_at REAL NOT NULL
);
"""


class MassiveDailyCache:
    """SQLite store of grouped daily sessions, shared by adapter instances."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path is not None else massive_cache_path()
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self._path, check_same_thread=False, timeout=15.0)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.executescript(_SCHEMA)
                connection.commit()
            except sqlite3.Error as exc:
                raise MassiveAPIError(
                    f"Massive daily cache at {self._path} is unavailable: {exc}"
                ) from exc
            self._connection = connection
        return self._connection

    def acquire_call_slot(
        self, min_interval_seconds: float, max_wait_seconds: float = 300.0
    ) -> None:
        """Reserve one API call slot across every process sharing this cache.

        The worker daemon and a manual backfill both rate-limit separately in
        memory, so they can exceed the plan limit together. This SQLite slot
        spreads calls at least `min_interval_seconds` apart machine-wide.
        """
        deadline = time.monotonic() + max_wait_seconds
        while True:
            try:
                with self._lock:
                    connection = self._connect()
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        row = connection.execute(
                            "SELECT last_call_at FROM rate_limit WHERE id = 1"
                        ).fetchone()
                        now = time.time()
                        last = float(row[0]) if row else 0.0
                        if now - last >= min_interval_seconds:
                            connection.execute(
                                "INSERT OR REPLACE INTO rate_limit VALUES (1, ?)", (now,)
                            )
                            connection.commit()
                            return
                        wait = min_interval_seconds - (now - last)
                    finally:
                        if connection.in_transaction:
                            connection.rollback()
            except sqlite3.Error as exc:
                raise MassiveAPIError(f"Massive rate limit slot failed: {exc}") from exc
            if time.monotonic() + wait > deadline:
                raise MassiveRateLimitError(
                    f"Massive rate limit slot would need {wait:.1f}s of waiting"
                )
            time.sleep(min(wait, 5.0) + 0.05)

    def fetched_dates(self) -> set[date]:
        try:
            with self._lock:
                rows = self._connect().execute("SELECT session_date FROM fetched_dates").fetchall()
        except sqlite3.Error as exc:
            raise MassiveAPIError(f"Massive daily cache read failed: {exc}") from exc
        sessions: set[date] = set()
        for (raw,) in rows:
            try:
                sessions.add(date.fromisoformat(str(raw)))
            except ValueError:
                continue
        return sessions

    def store_date(self, session: date, rows: list[dict[str, Any]]) -> None:
        stored: list[tuple[Any, ...]] = []
        for row in rows:
            symbol = str(row.get("T", "")).strip().upper()
            if not symbol or row.get("otc"):
                continue
            stored.append(
                (
                    session.isoformat(),
                    symbol,
                    _finite(row.get("o")),
                    _finite(row.get("h")),
                    _finite(row.get("l")),
                    _finite(row.get("c")),
                    _finite(row.get("v")),
                    _finite(row.get("vw")),
                    _finite(row.get("n")),
                )
            )
        try:
            with self._lock:
                connection = self._connect()
                connection.execute(
                    "INSERT OR REPLACE INTO fetched_dates VALUES (?, ?, ?)",
                    (
                        session.isoformat(),
                        len(rows),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                connection.executemany(
                    "INSERT OR REPLACE INTO daily_bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    stored,
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise MassiveAPIError(f"Massive daily cache write failed: {exc}") from exc

    def bars_for(self, symbols: set[str]) -> dict[str, list[tuple[date, dict[str, Any]]]]:
        clean = {symbol.strip().upper() for symbol in symbols if symbol.strip()}
        if not clean:
            return {}
        placeholders = ",".join("?" for _ in clean)
        try:
            with self._lock:
                rows = (
                    self._connect()
                    .execute(
                        "SELECT symbol, session_date, open, high, low, close, "
                        "volume, vwap, transactions FROM daily_bars "
                        f"WHERE symbol IN ({placeholders}) ORDER BY symbol, session_date",
                        tuple(sorted(clean)),
                    )
                    .fetchall()
                )
        except sqlite3.Error as exc:
            raise MassiveAPIError(f"Massive daily cache read failed: {exc}") from exc
        output: dict[str, list[tuple[date, dict[str, Any]]]] = {}
        for row in rows:
            try:
                session = date.fromisoformat(str(row[1]))
            except ValueError:
                continue
            output.setdefault(str(row[0]), []).append(
                (
                    session,
                    {
                        "open": row[2],
                        "high": row[3],
                        "low": row[4],
                        "close": row[5],
                        "volume": row[6],
                        "vwap": row[7],
                        "transactions": row[8],
                    },
                )
            )
        return output

    def prune_before(self, cutoff: date) -> int:
        try:
            with self._lock:
                connection = self._connect()
                bars = connection.execute(
                    "DELETE FROM daily_bars WHERE session_date < ?", (cutoff.isoformat(),)
                ).rowcount
                dates = connection.execute(
                    "DELETE FROM fetched_dates WHERE session_date < ?",
                    (cutoff.isoformat(),),
                ).rowcount
                connection.commit()
        except sqlite3.Error as exc:
            raise MassiveAPIError(f"Massive daily cache prune failed: {exc}") from exc
        return max(0, bars) + max(0, dates)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> MassiveDailyCache:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()


class MassiveClient:
    """Minimal Massive REST client for the grouped daily endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        api_root: str = MASSIVE_API_ROOT,
        limiter: RateLimiter | None = None,
        timeout: float = 15.0,
        fetch_recorder: SourceFetchRecorder | None = None,
        shared_cache: MassiveDailyCache | None = None,
        calls_per_minute: int = 5,
    ) -> None:
        self.api_key = api_key
        self.api_root = api_root.rstrip("/")
        self.limiter = limiter or RateLimiter()
        self.timeout = timeout
        self.fetch_recorder = fetch_recorder
        self.shared_cache = shared_cache
        self.calls_per_minute = max(1, calls_per_minute)

    def _record(self, fetch: SourceFetch) -> None:
        if self.fetch_recorder is None:
            return
        try:
            self.fetch_recorder(fetch)
        except Exception:  # ingestion must not break live quotes
            pass

    def _safe_error(self, error: object) -> str:
        """Return provider errors without ever repeating the API key."""

        message = str(error)
        encoded_key = urllib.parse.quote(self.api_key)
        for secret in {self.api_key, encoded_key}:
            if secret:
                message = message.replace(secret, "[redacted]")
        return message

    def _reserve_call(self) -> None:
        """Apply both the in-process bucket and the machine-wide slot."""
        self.limiter.acquire()
        if self.shared_cache is not None:
            self.shared_cache.acquire_call_slot(60.0 / self.calls_per_minute)

    def grouped_daily(self, session: date) -> list[dict[str, Any]]:
        path = GROUPED_DAILY_PATH.format(date=session.isoformat())
        safe_locator = f"{self.api_root}{path}?adjusted=true"
        request_url = safe_locator + f"&apiKey={urllib.parse.quote(self.api_key)}"
        attempts = 3
        for attempt in range(attempts):
            self._reserve_call()
            body = self._grouped_daily_request(session, safe_locator, request_url)
            if body is not None:
                return self._parse_grouped_daily(session, safe_locator, body)
            if attempt + 1 < attempts:
                # Back off hard before the retry; other processes on this
                # machine share the plan's call budget.
                time.sleep(60.0 * (attempt + 1))
        raise MassiveAPIError(
            f"Massive kept returning HTTP 429 for {session} after {attempts} attempts"
        )

    def _grouped_daily_request(
        self, session: date, safe_locator: str, request_url: str
    ) -> bytes | None:
        """Issue one grouped daily request; None means retry on rate limit."""
        started_at = datetime.now(UTC)
        request = urllib.request.Request(request_url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            self._record(
                SourceFetch.failure(
                    source="massive",
                    feed="market_bars",
                    locator=safe_locator,
                    started_at=started_at,
                    error=f"HTTP {exc.code}",
                    metadata={"session": session.isoformat()},
                )
            )
            if exc.code == 429:
                return None
            # HTTPError retains the full request URL, including credentials.
            # Do not attach it as a printable exception cause.
            raise MassiveAPIError(f"Massive returned HTTP {exc.code} for {session}") from None
        except urllib.error.URLError as exc:
            safe_error = self._safe_error(exc.reason)
            self._record(
                SourceFetch.failure(
                    source="massive",
                    feed="market_bars",
                    locator=safe_locator,
                    started_at=started_at,
                    error=safe_error,
                    metadata={"session": session.isoformat()},
                )
            )
            # URLError reasons can repeat the full URL. The safe outer error is
            # enough for operators and keeps tracebacks free of credentials.
            raise MassiveAPIError(
                f"Could not reach Massive for {session}: {safe_error}"
            ) from None

    def _parse_grouped_daily(
        self, session: date, safe_locator: str, body: bytes
    ) -> list[dict[str, Any]]:
        started_at = datetime.now(UTC)
        try:
            payload = json.loads(body)
        except ValueError as exc:
            self._record(
                SourceFetch.failure(
                    source="massive",
                    feed="market_bars",
                    locator=safe_locator,
                    started_at=started_at,
                    error=f"Invalid JSON: {exc}",
                    metadata={"session": session.isoformat()},
                )
            )
            raise MassiveAPIError(f"Massive returned invalid JSON for {session}") from exc

        status = payload.get("status")
        results = payload.get("results")
        if status == "OK" and results is None:
            # Market holidays return no results; cache them as empty sessions
            # so they are not refetched on every scan.
            results = []
        if status != "OK" or not isinstance(results, list):
            self._record(
                SourceFetch.failure(
                    source="massive",
                    feed="market_bars",
                    locator=safe_locator,
                    started_at=started_at,
                    error=f"status={status!r}",
                    metadata={"session": session.isoformat()},
                )
            )
            raise MassiveAPIError(
                f"Massive grouped daily response for {session} was not usable (status={status!r})"
            )
        self._record(
            SourceFetch.success(
                source="massive",
                feed="market_bars",
                locator=safe_locator,
                started_at=started_at,
                payload=body,
                content_type="application/json",
                metadata={
                    "session": session.isoformat(),
                    "returned_rows": len(results),
                },
                partial=not results,
            )
        )
        return results


class MassiveBarAdapter:
    """Provider adapter serving daily bars from the grouped daily cache."""

    name = "massive"
    capabilities = frozenset({DataKind.BARS})

    def __init__(
        self,
        client: MassiveClient,
        cache: MassiveDailyCache,
        *,
        max_fetch_calls: int = 10,
        today_et: date | None = None,
    ) -> None:
        self.client = client
        self.cache = cache
        self.max_fetch_calls = max_fetch_calls
        self.today_et = today_et

    def close(self) -> None:
        self.cache.close()

    def __enter__(self) -> MassiveBarAdapter:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def _today(self) -> date:
        if self.today_et is not None:
            return self.today_et
        return datetime.now(UTC).astimezone(EASTERN).date()

    def fetch(
        self,
        request: ProviderRequest,
        progress: ProgressCallback | None = None,
    ) -> FetchBatch:
        if request.kind != DataKind.BARS:
            raise ValueError("MassiveBarAdapter only supports bar requests")
        if request.interval != "1d":
            raise ValueError(
                "MassiveBarAdapter only serves daily bars; intraday bars stay on faster providers"
            )
        started_at = datetime.now(UTC)
        sessions = _session_dates(request.period, self._today())
        cached_sessions = self.cache.fetched_dates()
        uncached = [session for session in sessions if session not in cached_sessions]
        if len(uncached) > self.max_fetch_calls:
            raise MassiveAPIError(
                f"Massive daily cache needs {len(uncached)} uncached sessions "
                f"but the scan budget is {self.max_fetch_calls}; run "
                f"stonks-massive-backfill to warm the cache"
            )

        fetched_now: list[date] = []
        for session in sorted(uncached, reverse=True):
            if progress:
                progress(
                    len(fetched_now),
                    len(uncached),
                    f"Massive daily session {session}",
                )
            rows = self.client.grouped_daily(session)
            self.cache.store_date(session, rows)
            fetched_now.append(session)
        if progress and uncached:
            progress(len(uncached), len(uncached), "Massive daily sessions complete")

        by_symbol = self.cache.bars_for(set(request.symbols))
        bars: list[Bar] = []
        for symbol in sorted(set(request.symbols)):
            for session, row in by_symbol.get(symbol, []):
                bars.append(
                    Bar(
                        symbol=symbol,
                        interval="1d",
                        timestamp=datetime(
                            session.year, session.month, session.day, tzinfo=EASTERN
                        ),
                        open=_finite(row.get("open")),
                        high=_finite(row.get("high")),
                        low=_finite(row.get("low")),
                        close=_finite(row.get("close")),
                        volume=_finite(row.get("volume")),
                    )
                )
        collected_at = datetime.now(UTC)
        as_of = max((bar.timestamp for bar in bars), default=collected_at)
        returned_symbols = {bar.symbol for bar in bars}
        failed = sorted(set(request.symbols) - returned_symbols)
        status = "partial" if failed and bars else "success" if bars else "error"
        return FetchBatch(
            request=request,
            status=status,
            provenance=ProviderProvenance(
                provider=self.name,
                feed="market_bars",
                locator="massive://aggs/grouped/locale/us/market/stocks",
                observed_at=as_of if bars else None,
                as_of=as_of,
                collected_at=collected_at,
                delayed=True,
                quality={
                    "requested_symbols": len(request.symbols),
                    "returned_symbols": len(returned_symbols),
                    "failed_symbols": failed,
                    "sessions_needed": len(sessions),
                    "sessions_cached_total": len(cached_sessions),
                    "sessions_fetched_this_call": len(fetched_now),
                    "started_at": started_at.isoformat(),
                },
            ),
            bars=tuple(bars),
            error="Massive returned no usable bars" if not bars else None,
        )


def massive_bar_adapter(
    *,
    fetch_recorder: SourceFetchRecorder | None = None,
    api_key: str | None = None,
) -> MassiveBarAdapter | None:
    """Build the Massive adapter from an injected key or the environment."""
    key = (api_key or massive_api_key()).strip()
    if not massive_enabled(key):
        return None
    if not key:
        return None
    cache = MassiveDailyCache(massive_cache_path())
    return MassiveBarAdapter(
        client=MassiveClient(
            key,
            limiter=RateLimiter(massive_calls_per_minute()),
            timeout=massive_timeout_seconds(),
            fetch_recorder=fetch_recorder,
            shared_cache=cache,
            calls_per_minute=massive_calls_per_minute(),
        ),
        cache=cache,
        max_fetch_calls=massive_max_scan_calls(),
    )


def backfill_daily_cache(
    client: MassiveClient,
    cache: MassiveDailyCache,
    *,
    days: int = 365,
    budget: int | None = None,
    today_et: date | None = None,
    prune_days: int | None = 730,
    progress: ProgressCallback | None = None,
) -> dict[str, int]:
    """Warm the grouped daily cache for `days` of weekday sessions."""
    today = today_et or datetime.now(UTC).astimezone(EASTERN).date()
    if prune_days:
        cache.prune_before(today - timedelta(days=prune_days))
    sessions = _weekdays(today - timedelta(days=days), today - timedelta(days=1))
    cached = cache.fetched_dates()
    uncached = [session for session in sessions if session not in cached]
    if budget is not None:
        uncached = uncached[-budget:] if budget >= 0 else uncached
    fetched = 0
    for position, session in enumerate(uncached, start=1):
        if progress:
            progress(position - 1, len(uncached), f"Massive daily session {session}")
        rows = client.grouped_daily(session)
        cache.store_date(session, rows)
        fetched += 1
    if progress:
        progress(len(uncached), len(uncached), "Massive backfill complete")
    return {
        "sessions_needed": len(sessions),
        "sessions_cached_before": len(sessions) - len(uncached),
        "sessions_fetched": fetched,
    }


def refresh_massive_backfill() -> dict[str, int | bool]:
    """Warm the daily cache from the environment for the worker loop.

    Makes no API calls when the cache is already warm. Returns stats for the
    worker heartbeat state and closes its cache connection after each pass.
    """
    if not massive_enabled():
        return {"enabled": False}
    adapter = massive_bar_adapter()
    if adapter is None:  # pragma: no cover - enabled() guarantees a key
        return {"enabled": False}
    try:
        return backfill_daily_cache(
            adapter.client,
            adapter.cache,
            days=365,
            budget=massive_backfill_calls(),
            prune_days=730,
        )
    finally:
        adapter.cache.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="stonks-massive-backfill",
        description="Backfill the Massive grouped daily bar cache.",
    )
    parser.add_argument("--days", type=int, default=365, help="Calendar days to cover.")
    parser.add_argument(
        "--max-calls", type=int, default=None, help="Stop after this many API calls."
    )
    parser.add_argument(
        "--prune-days", type=int, default=730, help="Delete cache rows older than this."
    )
    args = parser.parse_args()
    if not massive_enabled():
        print("Massive is not enabled; set MASSIVE_API_KEY to use it.")
        return 1
    adapter = massive_bar_adapter()
    if adapter is None:  # pragma: no cover - enabled() guarantees a key
        print("Massive is not enabled; set MASSIVE_API_KEY to use it.")
        return 1
    with adapter:
        stats = backfill_daily_cache(
            adapter.client,
            adapter.cache,
            days=max(1, args.days),
            budget=args.max_calls,
            prune_days=args.prune_days,
            progress=lambda done, total, label: print(f"[{done}/{total}] {label}", flush=True),
        )
    print(
        "Backfill complete: "
        f"{stats['sessions_fetched']} sessions fetched, "
        f"{stats['sessions_cached_before']} already cached, "
        f"{stats['sessions_needed']} needed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
