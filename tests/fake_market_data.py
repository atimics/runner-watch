"""Synthetic market frames used only by scanner and ranker tests."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from runner_watch.market_data import DownloadResult, ProgressCallback

EASTERN = ZoneInfo("America/New_York")
FAKE_SYMBOLS = ["SPRK", "VOLT", "NOVA", "PULSE", "LIFT", "CALM", "DUSK", "TIDE"]


class FakeMarketData:
    """Deterministic market-shaped data that is never packaged with production code."""

    def __init__(self, now: datetime | None = None) -> None:
        self.now = (now or datetime.now(UTC)).astimezone(EASTERN)

    def _trading_days(self, count: int) -> list[datetime]:
        day = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
        values: list[datetime] = []
        while len(values) < count:
            if day.weekday() < 5:
                values.append(day)
            day -= timedelta(days=1)
        return list(reversed(values))

    def daily(
        self, tickers: list[str], progress: ProgressCallback | None = None
    ) -> DownloadResult:
        frames: dict[str, pd.DataFrame] = {}
        days = self._trading_days(24)
        for number, ticker in enumerate(tickers):
            seed = sum(ord(char) for char in ticker)
            rng = np.random.default_rng(seed)
            base = 2.0 + (seed % 1800) / 100
            moves = rng.normal(0.0005, 0.018, len(days)).cumsum()
            close = base * (1 + moves)
            volume = rng.integers(180_000, 2_000_000, len(days))
            frames[ticker] = pd.DataFrame(
                {
                    "Open": close * rng.uniform(0.985, 1.01, len(days)),
                    "High": close * rng.uniform(1.01, 1.04, len(days)),
                    "Low": close * rng.uniform(0.96, 0.995, len(days)),
                    "Close": close,
                    "Volume": volume,
                },
                index=pd.DatetimeIndex(days).tz_localize(None),
            )
            if progress:
                progress(number + 1, len(tickers), "Building test daily history")
        return DownloadResult(frames, [], [])

    def intraday(
        self, tickers: list[str], progress: ProgressCallback | None = None
    ) -> DownloadResult:
        frames: dict[str, pd.DataFrame] = {}
        days = self._trading_days(5)
        stage_strength = [1.11, 1.07, 1.04, 1.025, 1.015, 1.005, 0.995, 0.985]
        for ticker_number, ticker in enumerate(tickers):
            seed = sum(ord(char) for char in ticker)
            rng = np.random.default_rng(seed + 91)
            base = 2.0 + (seed % 1800) / 100
            blocks: list[pd.DataFrame] = []
            for day_number, day in enumerate(days):
                start = day.replace(hour=4, minute=0)
                stop_clock = (
                    self.now.time().replace(tzinfo=None)
                    if day.date() == self.now.date()
                    else time(20)
                )
                stop = day.replace(
                    hour=stop_clock.hour,
                    minute=(stop_clock.minute // 5) * 5,
                    second=0,
                    microsecond=0,
                )
                if stop < start:
                    stop = day.replace(hour=20)
                index = pd.date_range(start, stop, freq="5min", tz=EASTERN)
                if index.empty:
                    continue
                noise = rng.normal(0, 0.002, len(index)).cumsum()
                run = np.zeros(len(index))
                volume_multiplier = 1.0
                if day_number == len(days) - 1:
                    finish = stage_strength[ticker_number % len(stage_strength)]
                    run = np.linspace(0, max(finish - 1, 0), len(index)) ** 1.35
                    volume_multiplier = max(1.0, (finish - 0.98) * 28)
                close = base * (1 + noise + run)
                volume = rng.integers(100, 4_000, len(index)).astype(float)
                volume *= volume_multiplier
                if day_number == len(days) - 1 and len(volume) >= 4:
                    volume[-4:] *= 2.5
                    close[-4:] *= np.linspace(1, 1.035 + ticker_number * 0.001, 4)
                blocks.append(
                    pd.DataFrame(
                        {
                            "Open": close * 0.998,
                            "High": close * 1.004,
                            "Low": close * 0.996,
                            "Close": close,
                            "Volume": volume.astype(int),
                        },
                        index=index,
                    )
                )
            if blocks:
                frames[ticker] = pd.concat(blocks)
            if progress:
                progress(ticker_number + 1, len(tickers), "Building test intraday history")
        return DownloadResult(frames, [], [])
