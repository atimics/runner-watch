from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class ScanSettings:


    min_price: float = 0.50
    max_price: float = 50.00
    min_avg_volume: int = 100_000
    min_avg_dollar_volume: float = 500_000
    max_symbols: int = 300
    batch_size: int = 60
    top_n: int = 50
    include_funds: bool = False
    max_stale_minutes: int = 45
    crash_only: bool = False
    crash_drawdown_pct: float = 60.0

    def validate(self) -> None:
        if self.min_price < 0 or self.max_price <= self.min_price:
            raise ValueError("Price range is not valid.")
        if self.min_avg_volume < 0 or self.min_avg_dollar_volume < 0:
            raise ValueError("Volume filters cannot be negative.")
        if self.max_symbols < 1 or self.batch_size < 1 or self.top_n < 1:
            raise ValueError("Scan limits must be at least 1.")
        if not 0 < self.crash_drawdown_pct < 100:
            raise ValueError("Crash drawdown must be between 0 and 100 percent.")


@dataclass(slots=True)
class DailyProfile:
    ticker: str
    previous_close: float
    previous_high: float
    average_volume: float
    average_dollar_volume: float
    high_20d: float
    high_90d: float
    high_52w: float
    low_20d: float


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
    momentum_previous_5m_pct: float = 0.0
    momentum_acceleration_pct: float = 0.0
    intraday_volatility_pct: float = 0.0
    vwap_position_pct: float = 0.0
    pullback_from_high_pct: float = 0.0
    close_location: float = 0.5
    recent_dollar_volume: float = 0.0
    opening_range_position: float = 0.5
    opening_range_breakout_pct: float = 0.0
    support_distance_pct: float = 0.0
    support_strength: float = 0.0
    resistance_distance_pct: float = 0.0
    resistance_strength: float = 0.0
    fib_retracement_pct: float = 0.0
    fib_level_distance_pct: float = 0.0
    structure_available: bool = False
    fibonacci_available: bool = False
    drawdown_20d_pct: float = 0.0
    drawdown_90d_pct: float = 0.0
    drawdown_52w_pct: float = 0.0
    rebound_from_20d_low_pct: float = 0.0
    rug_score: float = 0.0
    rug_level: str = "LOW"
    trade_state: str = "WATCH"
    state_reason: str = "No entry state is confirmed."
    hard_veto: bool = False
    crash_candidate: bool = False
    scoring_version: str = "market_risk_v3"
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
    all_rows: list[RunnerSnapshot] = field(default_factory=list)

    @property
    def elapsed_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()
