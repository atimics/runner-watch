from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import pandas as pd
import yfinance as yf

ProgressCallback = Callable[[int, int, str], None]


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

    def __init__(self, batch_size: int = 60, timeout: float = 15.0) -> None:
        self.batch_size = batch_size
        self.timeout = timeout

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
                failed.extend(ticker for ticker in batch if ticker not in found)
            except Exception as exc:  # yfinance has changed exception types across releases
                failed.extend(batch)
                warnings.append(f"{label} batch {number} failed: {exc}")

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
