from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_watch.ingestion import SourceFetch, SourceFetchRecorder
from runner_web.db import connection

FINTEL_API_ROOT = "https://api.fintel.io"
FINTEL_SECURITY_ROOT = "https://fintel.io/ss/us"
Transport = Callable[[str, dict[str, str], float], Any]


def short_data_configured() -> bool:
    enabled = os.getenv("FINTEL_SHORT_DATA_ENABLED", "true").strip().lower()
    return enabled not in {"0", "false", "no", "off"} and bool(
        os.getenv("FINTEL_API_KEY", "").strip()
    )


def _number(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip().replace(",", "").removesuffix("%")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalized_key(value: Any) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum())


def _flatten(record: dict[str, Any]) -> dict[str, Any]:
    output = dict(record)
    for value in record.values():
        if isinstance(value, dict):
            for key, nested_value in value.items():
                output.setdefault(key, nested_value)
    return output


def _records(payload: Any) -> list[dict[str, Any]]:
    current = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(current, list):
        return [_flatten(item) for item in current if isinstance(item, dict)]
    if not isinstance(current, dict):
        return []
    for key in ("observations", "records", "results", "items", "history"):
        value = current.get(key)
        if isinstance(value, list):
            return [_flatten(item) for item in value if isinstance(item, dict)]
    return [_flatten(current)]


def _pick(record: dict[str, Any], *names: str) -> Any:
    normalized = {_normalized_key(key): value for key, value in record.items()}
    for name in names:
        value = normalized.get(_normalized_key(name))
        if value is not None:
            return value
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:80] if text else None


def _latest_record(payload: Any, date_names: tuple[str, ...]) -> dict[str, Any]:
    records = _records(payload)
    if not records:
        return {}
    return max(
        records,
        key=lambda row: _text(_pick(row, *date_names)) or "",
    )


@dataclass(frozen=True, slots=True)
class ShortData:
    ticker: str
    short_interest_pct_float: float | None = None
    short_interest_shares: float | None = None
    days_to_cover: float | None = None
    short_interest_settlement_date: str | None = None
    borrow_fee_pct: float | None = None
    shares_available: float | None = None
    borrow_observed_at: str | None = None
    source: str = "fintel"
    source_url: str | None = None
    collected_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def available(self) -> bool:
        return any(
            value is not None
            for value in (
                self.short_interest_pct_float,
                self.short_interest_shares,
                self.days_to_cover,
                self.borrow_fee_pct,
                self.shares_available,
            )
        )


@dataclass(frozen=True, slots=True)
class ShortDataFetchResult:
    data: ShortData
    fetches: tuple[SourceFetch, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ShortDataScanResult:
    rows: dict[str, ShortData]
    configured: bool
    refreshed: int
    covered: int
    warnings: tuple[str, ...] = ()


def _default_transport(url: str, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class FintelShortDataClient:
    """Read Fintel's documented short-interest and borrow endpoints."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 5.0,
        max_workers: int = 6,
        transport: Transport | None = None,
        fetch_recorder: SourceFetchRecorder | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.max_workers = max(1, max_workers)
        self.transport = transport or _default_transport
        self.fetch_recorder = fetch_recorder

    def _request(self, ticker: str, feed: str) -> tuple[Any | None, SourceFetch]:
        encoded = urllib.parse.quote(ticker.lower(), safe=".-")
        url = f"{FINTEL_API_ROOT}/v1/securities/US/{encoded}/{feed.replace('_', '-')}"
        started_at = datetime.now(UTC)
        metadata = {"requested_count": 1, "requested_tickers": [ticker]}
        try:
            payload = self.transport(
                url,
                {
                    "Accept": "application/json",
                    "User-Agent": "RunnerWatch/0.3 https://stonks.rati.foundation",
                    "X-API-KEY": self.api_key,
                },
                self.timeout,
            )
            records = _records(payload)
            return payload, SourceFetch.success(
                source="fintel",
                feed=feed,
                locator=url,
                started_at=started_at,
                payload=payload,
                content_type="application/json",
                metadata={**metadata, "received_count": len(records)},
                partial=not records,
            )
        except Exception as exc:
            if isinstance(exc, urllib.error.HTTPError):
                if exc.code == 401:
                    detail = "API key was rejected"
                elif exc.code == 403:
                    detail = "account is not entitled to this feed"
                elif exc.code == 404:
                    detail = "ticker was not found"
                else:
                    detail = f"HTTP {exc.code}"
            else:
                detail = str(exc)[:300] or type(exc).__name__
            return None, SourceFetch.failure(
                source="fintel",
                feed=feed,
                locator=url,
                started_at=started_at,
                error=detail,
                metadata=metadata,
            )

    def fetch(self, ticker: str) -> ShortDataFetchResult:
        symbol = ticker.strip().upper()
        short_payload, short_fetch = self._request(symbol, "short_interest")
        borrow_payload, borrow_fetch = self._request(symbol, "borrow_rate")
        short = _latest_record(
            short_payload,
            (
                "settlement_date",
                "short_interest_date",
                "effective_date",
                "market_date",
                "date",
            ),
        )
        borrow = _latest_record(
            borrow_payload,
            ("observed_at", "reported_at", "updated_at", "timestamp", "date"),
        )
        data = ShortData(
            ticker=symbol,
            short_interest_pct_float=_number(
                _pick(
                    short,
                    "short_interest_percent_float",
                    "short_interest_pct_float",
                    "short_percent_of_float",
                    "percent_float_short",
                    "short_interest_float_percent",
                )
            ),
            short_interest_shares=_number(
                _pick(
                    short,
                    "short_interest_shares",
                    "shares_short",
                    "short_shares",
                    "short_interest",
                )
            ),
            days_to_cover=_number(
                _pick(
                    short,
                    "days_to_cover",
                    "short_interest_ratio",
                    "short_ratio",
                )
            ),
            short_interest_settlement_date=_text(
                _pick(
                    short,
                    "settlement_date",
                    "short_interest_date",
                    "effective_date",
                    "market_date",
                    "date",
                )
            ),
            borrow_fee_pct=_number(
                _pick(
                    borrow,
                    "borrow_fee_rate_percent",
                    "borrow_fee_pct",
                    "borrow_fee_rate",
                    "fee_rate_percent",
                    "fee",
                )
            ),
            shares_available=_number(
                _pick(
                    borrow,
                    "shares_available_to_borrow",
                    "shares_available",
                    "available_shares",
                    "short_borrow_shares",
                    "availability",
                )
            ),
            borrow_observed_at=_text(
                _pick(
                    borrow,
                    "observed_at",
                    "reported_at",
                    "updated_at",
                    "timestamp",
                    "date",
                )
            ),
            source_url=f"{FINTEL_SECURITY_ROOT}/{urllib.parse.quote(symbol.lower(), safe='.-')}",
        )
        failed = sum(fetch.status == "error" for fetch in (short_fetch, borrow_fetch))
        warnings: list[str] = []
        if failed == 2:
            warnings.append(f"Fintel returned no short data for {symbol}.")
        elif failed or not data.available:
            warnings.append(f"Fintel returned partial short data for {symbol}.")
        return ShortDataFetchResult(
            data=data,
            fetches=(short_fetch, borrow_fetch),
            warnings=tuple(warnings),
        )

    def fetch_many(self, tickers: list[str]) -> tuple[dict[str, ShortData], list[str]]:
        unique = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker.strip()))
        output: dict[str, ShortData] = {}
        warnings: list[str] = []
        attempts: list[SourceFetch] = []
        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(unique) or 1)) as pool:
            futures = {pool.submit(self.fetch, ticker): ticker for ticker in unique}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # one ticker must not break a scan
                    warnings.append(f"Fintel short-data lookup failed for {ticker}: {exc}")
                    continue
                attempts.extend(result.fetches)
                warnings.extend(result.warnings)
                output[ticker] = result.data
        if self.fetch_recorder is not None:
            for fetch in attempts:
                try:
                    self.fetch_recorder(fetch)
                except Exception:
                    pass
        return output, warnings


def _parse_datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def load_cached_short_data(tickers: list[str]) -> dict[str, ShortData]:
    unique = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker.strip()))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    with connection() as database:
        rows = database.execute(
            f"""
            SELECT * FROM short_data_cache WHERE ticker IN ({placeholders})
            """,  # noqa: S608
            unique,
        ).fetchall()
    return {
        str(row["ticker"]): ShortData(
            ticker=str(row["ticker"]),
            short_interest_pct_float=_number(row["short_interest_pct_float"]),
            short_interest_shares=_number(row["short_interest_shares"]),
            days_to_cover=_number(row["days_to_cover"]),
            short_interest_settlement_date=_text(row["short_interest_settlement_date"]),
            borrow_fee_pct=_number(row["borrow_fee_pct"]),
            shares_available=_number(row["shares_available"]),
            borrow_observed_at=_text(row["borrow_observed_at"]),
            source=str(row["source"] or "fintel"),
            source_url=_text(row["source_url"]),
            collected_at=_parse_datetime(row["collected_at"]),
        )
        for row in rows
    }


def _merge_short_data(previous: ShortData | None, current: ShortData) -> ShortData:
    if previous is None:
        return current
    return ShortData(
        ticker=current.ticker,
        short_interest_pct_float=(
            current.short_interest_pct_float
            if current.short_interest_pct_float is not None
            else previous.short_interest_pct_float
        ),
        short_interest_shares=(
            current.short_interest_shares
            if current.short_interest_shares is not None
            else previous.short_interest_shares
        ),
        days_to_cover=(
            current.days_to_cover if current.days_to_cover is not None else previous.days_to_cover
        ),
        short_interest_settlement_date=(
            current.short_interest_settlement_date or previous.short_interest_settlement_date
        ),
        borrow_fee_pct=(
            current.borrow_fee_pct
            if current.borrow_fee_pct is not None
            else previous.borrow_fee_pct
        ),
        shares_available=(
            current.shares_available
            if current.shares_available is not None
            else previous.shares_available
        ),
        borrow_observed_at=current.borrow_observed_at or previous.borrow_observed_at,
        source=current.source,
        source_url=current.source_url or previous.source_url,
        collected_at=current.collected_at,
    )


def store_short_data(rows: dict[str, ShortData]) -> None:
    if not rows:
        return
    with connection() as database:
        database.executemany(
            """
            INSERT INTO short_data_cache(
                ticker,short_interest_pct_float,short_interest_shares,days_to_cover,
                short_interest_settlement_date,borrow_fee_pct,shares_available,
                borrow_observed_at,source,source_url,collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
                short_interest_pct_float=excluded.short_interest_pct_float,
                short_interest_shares=excluded.short_interest_shares,
                days_to_cover=excluded.days_to_cover,
                short_interest_settlement_date=excluded.short_interest_settlement_date,
                borrow_fee_pct=excluded.borrow_fee_pct,
                shares_available=excluded.shares_available,
                borrow_observed_at=excluded.borrow_observed_at,
                source=excluded.source,source_url=excluded.source_url,
                collected_at=excluded.collected_at
            """,
            [
                (
                    row.ticker,
                    row.short_interest_pct_float,
                    row.short_interest_shares,
                    row.days_to_cover,
                    row.short_interest_settlement_date,
                    row.borrow_fee_pct,
                    row.shares_available,
                    row.borrow_observed_at,
                    row.source,
                    row.source_url,
                    row.collected_at.isoformat(),
                )
                for row in rows.values()
            ],
        )


def short_data_for_scan(
    tickers: list[str],
    *,
    refresh_tickers: list[str] | None = None,
    as_of: datetime | None = None,
    client: FintelShortDataClient | None = None,
    fetch_recorder: SourceFetchRecorder | None = None,
) -> ShortDataScanResult:
    """Return cached positioning data and refresh the displayed scan rows."""

    checked_at = (as_of or datetime.now(UTC)).astimezone(UTC)
    unique = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker.strip()))
    cached = load_cached_short_data(unique)
    api_key = os.getenv("FINTEL_API_KEY", "").strip()
    source_enabled = (
        os.getenv("FINTEL_SHORT_DATA_ENABLED", "true").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    configured = source_enabled and (client is not None or bool(api_key))
    if not configured:
        return ShortDataScanResult(
            rows=cached,
            configured=False,
            refreshed=0,
            covered=sum(row.available for row in cached.values()),
        )

    ttl_seconds = max(60, int(os.getenv("FINTEL_SHORT_DATA_TTL_SECONDS", "900")))
    max_symbols = max(1, int(os.getenv("FINTEL_SHORT_DATA_MAX_SYMBOLS", "40")))
    refresh_candidates = list(
        dict.fromkeys(
            ticker.strip().upper()
            for ticker in (refresh_tickers if refresh_tickers is not None else unique)
            if ticker.strip()
        )
    )
    stale = [
        ticker
        for ticker in refresh_candidates
        if ticker not in cached
        or cached[ticker].collected_at < checked_at - timedelta(seconds=ttl_seconds)
    ][:max_symbols]
    if not stale:
        return ShortDataScanResult(
            rows=cached,
            configured=True,
            refreshed=0,
            covered=sum(row.available for row in cached.values()),
        )

    active_client = client or FintelShortDataClient(
        api_key,
        timeout=max(1.0, float(os.getenv("FINTEL_SHORT_DATA_TIMEOUT_SECONDS", "5"))),
        max_workers=max(1, int(os.getenv("FINTEL_SHORT_DATA_WORKERS", "6"))),
        fetch_recorder=fetch_recorder,
    )
    fresh, raw_warnings = active_client.fetch_many(stale)
    merged = {
        ticker: _merge_short_data(cached.get(ticker), row) for ticker, row in fresh.items()
    }
    store_short_data(merged)
    cached.update(merged)
    available_fresh = sum(row.available for row in fresh.values())
    missing = len(stale) - available_fresh
    warnings: list[str] = []
    if missing:
        warnings.append(
            f"Fintel short data was unavailable for {missing} of {len(stale)} refreshed symbols."
        )
    elif raw_warnings:
        warnings.append("Fintel returned partial short data for some symbols.")
    return ShortDataScanResult(
        rows=cached,
        configured=True,
        refreshed=len(fresh),
        covered=sum(row.available for row in cached.values()),
        warnings=tuple(warnings),
    )
