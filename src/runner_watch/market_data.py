from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from runner_watch.ingestion import SourceFetch, SourceFetchRecorder
from runner_watch.massive_data import massive_bar_adapter
from runner_watch.provider_contracts import (
    Bar,
    DataKind,
    FetchBatch,
    ProviderProvenance,
    ProviderRequest,
)
from runner_watch.provider_registry import ProviderRegistry, ProvidersExhaustedError

ProgressCallback = Callable[[int, int, str], None]
BarRecorder = Callable[[str, dict[str, pd.DataFrame]], None]
EASTERN = ZoneInfo("America/New_York")

_DAILY_CACHE_LOCK = threading.Lock()
_DAILY_CACHE_DAY: date | None = None
_DAILY_FRAME_CACHE: dict[str, pd.DataFrame] = {}
_DAILY_CACHE_PROVENANCE: ProviderProvenance | None = None


@dataclass(slots=True)
class DownloadResult:
    frames: dict[str, pd.DataFrame]
    failed: list[str]
    warnings: list[str]
    provenance: ProviderProvenance | None = None


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def split_download_frame(raw: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Split any common yfinance download shape into one frame per ticker."""

    if raw is None or raw.empty:
        return {}
    clean_tickers = [ticker.upper() for ticker in tickers]
    output: dict[str, pd.DataFrame] = {}

    if not isinstance(raw.columns, pd.MultiIndex):
        if len(clean_tickers) == 1:
            output[clean_tickers[0]] = raw.copy()
        return output

    level_zero = {str(value).upper() for value in raw.columns.get_level_values(0)}
    level_one = {str(value).upper() for value in raw.columns.get_level_values(1)}
    for ticker in clean_tickers:
        try:
            if ticker in level_zero:
                frame = raw.xs(ticker, axis=1, level=0, drop_level=True)
            elif ticker in level_one:
                frame = raw.xs(ticker, axis=1, level=1, drop_level=True)
            else:
                continue
        except (KeyError, ValueError):
            continue
        frame = frame.dropna(how="all")
        if not frame.empty:
            output[ticker] = frame
    return output


class YahooMarketData:
    """Small batching wrapper around yfinance."""

    def __init__(
        self,
        batch_size: int = 60,
        timeout: float = 15.0,
        recorder: BarRecorder | None = None,
        fetch_recorder: SourceFetchRecorder | None = None,
    ) -> None:
        self.batch_size = batch_size
        self.timeout = timeout
        self.recorder = recorder
        self.fetch_recorder = fetch_recorder

    def _record_fetch(self, fetch: SourceFetch, warnings: list[str]) -> None:
        if self.fetch_recorder is None:
            return
        try:
            self.fetch_recorder(fetch)
        except Exception as exc:  # ingestion must not break live quotes
            warnings.append(f"Could not record {fetch.source} {fetch.feed} fetch: {exc}")

    def _download(
        self,
        tickers: list[str],
        *,
        period: str,
        interval: str,
        prepost: bool,
        progress: ProgressCallback | None = None,
        label: str,
    ) -> DownloadResult:
        frames: dict[str, pd.DataFrame] = {}
        failed: list[str] = []
        warnings: list[str] = []
        groups = list(_chunks(tickers, self.batch_size))
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)

        for number, batch in enumerate(groups, start=1):
            started_at = datetime.now(UTC)
            if progress:
                progress(number - 1, len(groups), f"{label} batch {number}/{len(groups)}")
            try:
                raw: Any = yf.download(
                    tickers=batch,
                    period=period,
                    interval=interval,
                    group_by="ticker",
                    auto_adjust=False,
                    actions=False,
                    prepost=prepost,
                    threads=min(12, len(batch)),
                    progress=False,
                    timeout=self.timeout,
                    multi_level_index=True,
                )
                found = split_download_frame(raw, batch)
                frames.update(found)
                missing = [ticker for ticker in batch if ticker not in found]
                self._record_fetch(
                    SourceFetch.success(
                        source="yahoo",
                        feed="market_bars",
                        locator=f"yfinance://download/{interval}",
                        started_at=started_at,
                        payload=found,
                        content_type="application/x-pandas-frames",
                        metadata={
                            "period": period,
                            "interval": interval,
                            "prepost": prepost,
                            "requested_tickers": batch,
                            "returned_tickers": sorted(found),
                            "missing_tickers": missing,
                            "batch_number": number,
                            "batch_count": len(groups),
                        },
                        partial=bool(missing),
                    ),
                    warnings,
                )
                if self.recorder and found:
                    try:
                        self.recorder(interval, found)
                    except Exception as exc:  # collection must not break live quotes
                        warnings.append(f"Could not store {label.lower()} bars: {exc}")
                failed.extend(missing)
            except Exception as exc:  # yfinance has changed exception types across releases
                failed.extend(batch)
                warnings.append(f"{label} batch {number} failed: {exc}")
                self._record_fetch(
                    SourceFetch.failure(
                        source="yahoo",
                        feed="market_bars",
                        locator=f"yfinance://download/{interval}",
                        started_at=started_at,
                        error=exc,
                        metadata={
                            "period": period,
                            "interval": interval,
                            "prepost": prepost,
                            "requested_tickers": batch,
                            "batch_number": number,
                            "batch_count": len(groups),
                        },
                    ),
                    warnings,
                )

        if progress:
            progress(len(groups), len(groups), f"{label} complete")
        return DownloadResult(frames=frames, failed=failed, warnings=warnings)

    def daily(self, tickers: list[str], progress: ProgressCallback | None = None) -> DownloadResult:
        return self._download(
            tickers,
            period="1y",
            interval="1d",
            prepost=False,
            progress=progress,
            label="Daily history",
        )

    def intraday(
        self, tickers: list[str], progress: ProgressCallback | None = None
    ) -> DownloadResult:
        return self._download(
            tickers,
            period="5d",
            interval="5m",
            prepost=True,
            progress=progress,
            label="5-minute history",
        )


def _optional_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _canonical_timestamp(value: Any) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("America/New_York")
    return timestamp.tz_convert(UTC).to_pydatetime()


def _frame_bars(symbol: str, interval: str, frame: pd.DataFrame) -> list[Bar]:
    columns = {str(column).lower().replace(" ", ""): column for column in frame.columns}
    output: list[Bar] = []
    for index, row in frame.iterrows():
        output.append(
            Bar(
                symbol=symbol,
                interval=interval,
                timestamp=_canonical_timestamp(index),
                open=_optional_number(row.get(columns.get("open"))),
                high=_optional_number(row.get(columns.get("high"))),
                low=_optional_number(row.get(columns.get("low"))),
                close=_optional_number(row.get(columns.get("close"))),
                volume=_optional_number(row.get(columns.get("volume"))),
            )
        )
    return output


class YahooBarAdapter:
    """Turns Yahoo frames into the provider-neutral bar contract."""

    name = "yahoo"
    capabilities = frozenset({DataKind.BARS})

    def __init__(self, client: YahooMarketData | None = None) -> None:
        self.client = client or YahooMarketData()

    def fetch(
        self,
        request: ProviderRequest,
        progress: ProgressCallback | None = None,
    ) -> FetchBatch:
        if request.kind != DataKind.BARS:
            raise ValueError("YahooBarAdapter only supports bar requests")
        started_at = datetime.now(UTC)
        if request.interval == "1d":
            result = self.client.daily(list(request.symbols), progress)
        elif request.interval == "5m":
            result = self.client.intraday(list(request.symbols), progress)
        else:
            raise ValueError(f"Yahoo bar interval {request.interval!r} is not configured")

        try:
            bars = tuple(
                bar
                for symbol, frame in sorted(result.frames.items())
                for bar in _frame_bars(symbol, request.interval, frame)
            )
        except Exception as exc:
            self.client._record_fetch(
                SourceFetch.failure(
                    source=self.name,
                    feed="market_bars",
                    locator=f"yfinance://download/{request.interval}",
                    started_at=started_at,
                    error=f"Canonical bar conversion failed: {exc}",
                    metadata={
                        "interval": request.interval,
                        "requested_tickers": list(request.symbols),
                        "stage": "canonical_transform",
                    },
                ),
                result.warnings,
            )
            raise
        collected_at = datetime.now(UTC)
        as_of = max((bar.timestamp for bar in bars), default=collected_at)
        status = "partial" if result.failed and bars else "success" if bars else "error"
        return FetchBatch(
            request=request,
            status=status,
            provenance=ProviderProvenance(
                provider=self.name,
                feed="market_bars",
                locator=f"yfinance://download/{request.interval}",
                observed_at=as_of if bars else None,
                as_of=as_of,
                collected_at=collected_at,
                delayed=True,
                warnings=tuple(result.warnings),
                quality={
                    "requested_symbols": len(request.symbols),
                    "returned_symbols": len(result.frames),
                    "failed_symbols": sorted(result.failed),
                    "started_at": started_at.isoformat(),
                },
            ),
            bars=bars,
            error="Yahoo returned no usable bars" if not bars else None,
        )


def _bars_to_frames(batch: FetchBatch) -> dict[str, pd.DataFrame]:
    rows: dict[str, list[dict[str, Any]]] = {}
    for bar in batch.bars:
        rows.setdefault(bar.symbol, []).append(
            {
                "timestamp": bar.timestamp,
                "Open": bar.open,
                "High": bar.high,
                "Low": bar.low,
                "Close": bar.close,
                "Volume": bar.volume,
            }
        )
    frames: dict[str, pd.DataFrame] = {}
    for symbol, values in rows.items():
        frame = pd.DataFrame(values).set_index("timestamp").sort_index()
        frames[symbol] = frame
    return frames


class RoutedMarketData:
    """Scanner-compatible view over the canonical provider registry."""

    def __init__(
        self,
        registry: ProviderRegistry,
        intraday_registry: ProviderRegistry | None = None,
    ) -> None:
        # The daily registry may include slower end-of-day providers (Massive);
        # intraday scans stay on providers that serve the live session.
        self.registry = registry
        self.intraday_registry = intraday_registry or registry
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self.registry.close()
        if self.intraday_registry is not self.registry:
            self.intraday_registry.close()
        self._closed = True

    def __enter__(self) -> RoutedMarketData:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def _fetch(
        self,
        tickers: list[str],
        interval: str,
        progress: ProgressCallback | None,
    ) -> DownloadResult:
        registry = self.intraday_registry if interval != "1d" else self.registry
        request = ProviderRequest(
            kind=DataKind.BARS,
            symbols=tickers,
            interval=interval,
            period="1y" if interval == "1d" else "5d",
            extended_hours=interval != "1d",
        )
        try:
            batch = registry.fetch(request, progress=progress)
        except ProvidersExhaustedError as exc:
            return DownloadResult(
                frames={},
                failed=list(request.symbols),
                warnings=[str(exc)],
            )
        frames = _bars_to_frames(batch)
        failed = [symbol for symbol in request.symbols if symbol not in frames]
        warnings = list(batch.provenance.warnings)
        if batch.provenance.fallback_used:
            warnings.append(
                f"Used {batch.provenance.provider} after "
                f"{', '.join(batch.provenance.attempted_providers[:-1])} failed."
            )
        return DownloadResult(
            frames=frames,
            failed=failed,
            warnings=warnings,
            provenance=batch.provenance,
        )

    def daily(self, tickers: list[str], progress: ProgressCallback | None = None) -> DownloadResult:
        global _DAILY_CACHE_DAY, _DAILY_CACHE_PROVENANCE

        # Completed daily bars stay fixed during one Eastern trading day. This
        # cache is module-wide because web scans create a new routed client.
        symbols = list(
            dict.fromkeys(ticker.strip().upper() for ticker in tickers if ticker.strip())
        )
        if not symbols:
            return DownloadResult(frames={}, failed=[], warnings=[])

        cache_day = datetime.now(UTC).astimezone(EASTERN).date()
        with _DAILY_CACHE_LOCK:
            if _DAILY_CACHE_DAY != cache_day:
                _DAILY_FRAME_CACHE.clear()
                _DAILY_CACHE_PROVENANCE = None
                _DAILY_CACHE_DAY = cache_day

            missing = [symbol for symbol in symbols if symbol not in _DAILY_FRAME_CACHE]
            fresh = self._fetch(missing, "1d", progress) if missing else None
            if fresh is not None:
                _DAILY_FRAME_CACHE.update(fresh.frames)
                if fresh.provenance is not None:
                    _DAILY_CACHE_PROVENANCE = fresh.provenance
            elif progress:
                progress(1, 1, "Daily history cache")

            frames = {
                symbol: _DAILY_FRAME_CACHE[symbol]
                for symbol in symbols
                if symbol in _DAILY_FRAME_CACHE
            }
            provenance = fresh.provenance if fresh is not None else _DAILY_CACHE_PROVENANCE
            if provenance is not None:
                provenance = provenance.model_copy(
                    update={
                        "quality": {
                            **provenance.quality,
                            "requested_symbols": len(symbols),
                            "cache_hit_symbols": len(symbols) - len(missing),
                        }
                    }
                )
            return DownloadResult(
                frames=frames,
                failed=[symbol for symbol in symbols if symbol not in frames],
                warnings=list(fresh.warnings) if fresh is not None else [],
                provenance=provenance,
            )

    def intraday(
        self, tickers: list[str], progress: ProgressCallback | None = None
    ) -> DownloadResult:
        return self._fetch(tickers, "5m", progress)


def routed_market_data(
    *,
    batch_size: int = 60,
    timeout: float = 15.0,
    fetch_recorder: SourceFetchRecorder | None = None,
    provider_keys: Mapping[str, str] | None = None,
    provider_order: list[str] | None = None,
) -> RoutedMarketData:
    yahoo = YahooBarAdapter(
        YahooMarketData(
            batch_size=batch_size,
            timeout=timeout,
            fetch_recorder=fetch_recorder,
        )
    )
    daily = ProviderRegistry()
    intraday = ProviderRegistry()
    massive = massive_bar_adapter(
        fetch_recorder=fetch_recorder,
        api_key=(provider_keys or {}).get("massive"),
    )
    available = {"yahoo": yahoo}
    if massive is not None:
        available["massive"] = massive
    requested = [
        provider.strip().lower()
        for provider in (provider_order or ["massive", "yahoo"])
        if provider.strip().lower() in available
    ]
    ordered = list(dict.fromkeys([*requested, *available]))
    for name in ordered:
        daily.register(available[name])
    daily.route(DataKind.BARS, *ordered)
    intraday.register(yahoo)
    intraday.route(DataKind.BARS, "yahoo")
    return RoutedMarketData(daily, intraday_registry=intraday)
