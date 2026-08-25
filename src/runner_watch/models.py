from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ScanSettings:
    """Filters and limits for one scan."""

    min_price: float = 0.50
    max_price: float = 50.00
    min_avg_volume: int = 100_000
    min_avg_dollar_volume: float = 500_000
    max_symbols: int = 300
    batch_size: int = 60
    top_n: int = 50
    include_funds: bool = False
    max_stale_minutes: int = 45

    def validate(self) -> None:
        if self.min_price < 0 or self.max_price <= self.min_price:
            raise ValueError("Price range is not valid.")
        if self.min_avg_volume < 0 or self.min_avg_dollar_volume < 0:
            raise ValueError("Volume filters cannot be negative.")
        if self.max_symbols < 1 or self.batch_size < 1 or self.top_n < 1:
            raise ValueError("Scan limits must be at least 1.")


@dataclass(slots=True)
class DailyProfile:
    ticker: str
    previous_close: float
    previous_high: float
    average_volume: float
    average_dollar_volume: float


@dataclass(slots=True)
class RunnerSnapshot:
    ticker: str
    score: float
    stage: str
    session: str
    price: float
    change_pct: float
    momentum_5m_pct: float
    momentum_15m_pct: float
    relative_volume: float | None
    recent_relative_volume: float | None
    breakout_pct: float
    range_position: float
    session_volume: int
    dollar_volume: float
    average_volume: int
    average_dollar_volume: float
    quote_time: datetime
    stale_minutes: float
    signals: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["quote_time"] = self.quote_time.isoformat()
        return value


@dataclass(slots=True)
class ScanResult:
    rows: list[RunnerSnapshot]
    requested_symbols: int
    liquid_symbols: int
    scanned_symbols: int
    failed_symbols: list[str]
    warnings: list[str]
    started_at: datetime
    finished_at: datetime

    @property
    def elapsed_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

