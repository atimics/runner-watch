from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111112"
STRATEGY = "solana-setup-v1"
PAPER_MODEL = "jupiter-delay-haircut-v1"


def stamp(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).astimezone(UTC).isoformat()


def age(value: object, at: datetime) -> float:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (at - when).total_seconds() if when.tzinfo else float("inf")
    except (TypeError, ValueError):
        return float("inf")


def number(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("A finite number is required") from None
    if not result.is_finite():
        raise ValueError("A finite number is required")
    return result


@dataclass(frozen=True)
class Policy:
    strategy: str = STRATEGY
    paper_model: str = PAPER_MODEL
    initial_usdc: int = 1_000_000_000
    initial_sol: int = 50_000_000
    entry_usdc: int = 25_000_000
    max_positions: int = 4
    max_exposure_usdc: int = 100_000_000
    daily_loss_usdc: int = 30_000_000
    max_drawdown_bps: int = 1_000
    stop_loss_bps: int = 1_500
    take_profit_bps: int = 3_000
    max_hold_seconds: int = 21_600
    min_real_liquidity_usdc: int = 25_000_000_000
    max_entry_pool_bps: int = 50
    max_price_impact_bps: int = 100
    max_round_trip_loss_bps: int = 500
    slippage_bps: int = 100
    paper_haircut_bps: int = 50
    paper_delay_seconds: int = 3
    quote_max_age_seconds: int = 10
    evidence_max_age_seconds: int = 300
    holder_max_age_seconds: int = 21_600
    min_sol_lamports: int = 10_000_000
    max_fee_lamports: int = 5_000_000
    paper_fee_lamports: int = 100_000
    entry_timeout_seconds: int = 180
    cycle_seconds: int = 30
    max_new_signals: int = 10

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if name in {"strategy", "paper_model"}:
                continue
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.strategy != STRATEGY or self.paper_model != PAPER_MODEL:
            raise ValueError("A supported strategy and fill model are required")
        if not self.entry_usdc <= self.max_exposure_usdc <= self.initial_usdc:
            raise ValueError("Entry, exposure and capital limits must be in order")
        if self.initial_usdc > 100_000_000_000 or self.max_positions > 20:
            raise ValueError("Capital or position limit exceeds this release's bounds")
        if self.cycle_seconds < 15 or self.max_new_signals > 20:
            raise ValueError("Use a cycle of at least 15 seconds and at most 20 new signals")
        if (
            self.paper_fee_lamports > self.max_fee_lamports
            or self.initial_sol < self.min_sol_lamports
        ):
            raise ValueError("Fee and SOL reserve limits must be in order")
        for name in (
            "slippage_bps",
            "paper_haircut_bps",
            "max_price_impact_bps",
            "max_round_trip_loss_bps",
            "stop_loss_bps",
            "max_drawdown_bps",
        ):
            if getattr(self, name) >= 10_000:
                raise ValueError(f"{name} must be below 10000")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.json().encode()).hexdigest()

    def json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_env(cls) -> Policy:
        body = os.getenv("TRADING_POLICY_JSON", "").strip()
        return cls(**json.loads(body)) if body else cls()


def mode() -> str:
    value = os.getenv("TRADING_MODE", "paper")
    if value not in {"paper", "live"}:
        raise ValueError("TRADING_MODE must be paper or live")
    if value == "live" and os.getenv("TRADING_LIVE_ENABLED") != "1":
        raise ValueError("Live execution needs TRADING_LIVE_ENABLED=1")
    return value


def account_id(value: str) -> str:
    if value not in {"paper", "live"}:
        raise ValueError("Unknown trading account")
    return f"solana-{value}-v1"
