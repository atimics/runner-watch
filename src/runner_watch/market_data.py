from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import yfinance as yf

from runner_watch.ingestion import SourceFetch, SourceFetchRecorder

ProgressCallback = Callable[[int, int, str], None]
BarRecorder = Callable[[str, dict[str, pd.DataFrame]], None]


@dataclass(slots=True)
class DownloadResult:
    frames: dict[str, pd.DataFrame]
    failed: list[str]
    warnings: list[str]


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

    def daily(
        self, tickers: list[str], progress: ProgressCallback | None = None
    ) -> DownloadResult:
        return self._download(
            tickers,
            period="1mo",
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
