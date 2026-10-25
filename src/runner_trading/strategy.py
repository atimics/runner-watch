from __future__ import annotations

from datetime import datetime
from typing import Any

from runner_trading.config import Policy, age, number


def entry_reasons(row: dict[str, Any], policy: Policy, at: datetime) -> list[str]:
    """All entry gates use dated evidence. Exit decisions have their own rules."""
    reasons = []
    if row.get("network") != "solana" or row.get("venue") != "pool":
        reasons.append("A graduated Solana pool is required")
    if (row.get("early") or {}).get("state") != "setup":
        reasons.append("Waiting for SETUP")
    if not 0 <= age(row.get("observed_at"), at) <= policy.evidence_max_age_seconds:
        reasons.append("Refresh the price evidence")
    ratification = row.get("ratification") or {}
    if not ratification.get("ratified"):
        reasons.append("Complete the nine risk checks")
    if not 0 <= age(ratification.get("as_of"), at) <= policy.evidence_max_age_seconds:
        reasons.append("Refresh the risk checks")
    assessment = row.get("memecoin_assessment") or {}
    coverage = assessment.get("coverage") or {}
    if not 0 <= age(coverage.get("chain_checked_at"), at) <= policy.evidence_max_age_seconds:
        reasons.append("Refresh chain coverage")
    if (assessment.get("risk") or {}).get("factors"):
        reasons.append("A chain risk finding is active")
    checks = row.get("trading_checks") or {}
    if (
        checks.get("holder_rules") != 7
        or not 0 <= age(checks.get("holder_checked_at"), at) <= policy.holder_max_age_seconds
    ):
        reasons.append("Refresh wallet concentration with rules 7")
    if not checks.get("controls_clean"):
        reasons.append("Read mint and freeze controls")
    if not 0 <= age(checks.get("lock_checked_at"), at) <= 3600:
        reasons.append("Refresh the pool lock check")
    if (assessment.get("sentiment") or {}).get("observed_swaps", 0) < 3:
        reasons.append("Sample at least three recent swaps")
    try:
        liquidity = number(row.get("real_liquidity_usd")) * 1_000_000
        if liquidity < policy.min_real_liquidity_usdc:
            reasons.append("Real liquidity is below the strategy limit")
        if policy.entry_usdc * 10_000 > liquidity * policy.max_entry_pool_bps:
            reasons.append("Entry is too large for the pool")
    except ValueError:
        reasons.append("Read real pool liquidity")
    return reasons


def exit_reason(
    position: dict[str, Any],
    row: dict[str, Any] | None,
    net_value: int | None,
    policy: Policy,
    at: datetime,
) -> str | None:
    if position.get("exit_requested"):
        return "Operator requested exit"
    if age(position["opened_at"], at) >= policy.max_hold_seconds:
        return "Holding time reached"
    if row and (row.get("early") or {}).get("state") == "avoid":
        return "Risk state changed to AVOID"
    if row and ((row.get("memecoin_assessment") or {}).get("risk") or {}).get("factors"):
        return "Chain risk finding"
    if net_value is not None:
        cost = position["cost_usdc"] + position["entry_fee_usdc"]
        if net_value * 10_000 <= cost * (10_000 - policy.stop_loss_bps):
            return "Loss limit reached"
        if net_value * 10_000 >= cost * (10_000 + policy.take_profit_bps):
            return "Profit target reached"
    return None
