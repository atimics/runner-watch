"""Batch-local indexes over archived prices, without a cross-request price cache.

The collector shares one immutable history per ticker across many snapshots.
Sorting and session conversion happen once; time-window lookups are logarithmic.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import overload
from zoneinfo import ZoneInfo

Bar = tuple[datetime, float, float, float]
_EASTERN = ZoneInfo("America/New_York")


class IndexedBars(Sequence[Bar]):
    """Chronological, immutable bars and the last observation on each session date.

    Stable sorting retains duplicate-timestamp order. Session closes deliberately
    mean the last *available* observation, exactly as the legacy collector did;
    this does not reinterpret sparse data as an official exchange close.
    """

    __slots__ = ("_bars", "_times", "_session_index")

    def __init__(self, bars: Iterable[Bar]) -> None:
        self._bars = tuple(sorted(bars, key=lambda bar: bar[0]))
        self._times = tuple(bar[0] for bar in self._bars)
        # Barrier-only consumers need no per-bar timezone conversion.
        self._session_index: tuple[tuple[date, ...], tuple[Bar, ...]] | None = None

    def __len__(self) -> int:
        return len(self._bars)

    def __iter__(self) -> Iterator[Bar]:
        return iter(self._bars)

    @overload
    def __getitem__(self, index: int) -> Bar: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[Bar, ...]: ...

    def __getitem__(self, index: int | slice) -> Bar | tuple[Bar, ...]:
        return self._bars[index]

    def between(self, start: datetime, end: datetime) -> list[Bar]:
        """Return (start, end], retaining the collector's exact endpoint convention."""
        left = bisect_right(self._times, start.astimezone(UTC))
        right = bisect_right(self._times, end.astimezone(UTC))
        return list(self._bars[left:right])

    def first_at_or_after(self, target: datetime) -> Bar | None:
        index = bisect_left(self._times, target.astimezone(UTC))
        return self._bars[index] if index < len(self._bars) else None

    def near(self, target: datetime, tolerance: timedelta) -> tuple[float, datetime] | None:
        """Prefer the first bar at/after target, otherwise the last bar before it."""
        target = target.astimezone(UTC)
        index = bisect_left(self._times, target)
        if index < len(self._bars) and self._times[index] <= target + tolerance:
            stamp, _, _, close = self._bars[index]
            return close, stamp
        if index > 0 and self._times[index - 1] >= target - tolerance:
            stamp, _, _, close = self._bars[index - 1]
            return close, stamp
        return None

    def session_close(self, base_date: date, offset: int) -> tuple[float, datetime] | None:
        if offset < 0:
            raise ValueError("Session offset must be non-negative")
        sessions = self._session_index
        if sessions is None:
            last_by_date: dict[date, Bar] = {}
            for bar in self._bars:
                last_by_date[bar[0].astimezone(_EASTERN).date()] = bar
            dates = tuple(sorted(last_by_date))
            sessions = (dates, tuple(last_by_date[day] for day in dates))
            self._session_index = sessions
        dates, closes = sessions
        index = bisect_right(dates, base_date) + offset
        if index >= len(closes):
            return None
        stamp, _, _, close = closes[index]
        return close, stamp
