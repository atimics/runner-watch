from __future__ import annotations

from dataclasses import dataclass, field


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class RiskInput:
    """Point-in-time facts used to judge rug risk and trade state.

    Missing facts stay missing. The engine never turns an unknown fundamental
    into a bullish assumption.
    """

    setup_score: float
    price: float
    change_pct: float
    momentum_5m_pct: float
    momentum_15m_pct: float
    vwap_position_pct: float
    pullback_from_high_pct: float
    close_location: float
    dollar_volume: float
    recent_dollar_volume: float
    stale_minutes: float
    drawdown_20d_pct: float = 0.0
    drawdown_90d_pct: float = 0.0
    drawdown_52w_pct: float = 0.0
    rebound_from_20d_low_pct: float = 0.0
    filing_form: str | None = None
    filing_sentiment: str | None = None
    filing_kind: str | None = None
    active_halt: bool = False
    going_concern: bool = False
    reverse_split_count_1y: int = 0
    shares_growth_pct: float | None = None
    cash_runway_months: float | None = None
    current_ratio: float | None = None
    debt_to_cash: float | None = None
    issuer_data_available: bool | None = None
    institutional_ownership_pct: float | None = None
    institutional_change_pct: float | None = None
    institutional_data_age_days: float | None = None
    beneficial_ownership_pct: float | None = None
    previous_trade_state: str | None = None


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    rug_score: float
    rug_level: str
    trade_state: str
    state_reason: str
    hard_veto: bool
    crash_candidate: bool
    risk_reasons: list[str] = field(default_factory=list)


def _filing_flags(item: RiskInput) -> tuple[float, list[str], bool]:
    form = (item.filing_form or "").upper()
    kind = (item.filing_kind or "").lower()
    score = 0.0
    reasons: list[str] = []
    hard_veto = False

    if form.startswith(("S-1", "S-3", "424B", "POS AM")):
        score += 30
        reasons.append("exit-liquidity pipe may be open")
    elif form.startswith("EFFECT"):
        score += 25
        reasons.append("registration is effective; new supply can hit")
    elif form.startswith("144"):
        score += 16
        reasons.append("holder reported a possible sale")
    elif form.startswith(("NT 10-Q", "NT 10-K")):
        score += 24
        reasons.append("financial report is late")
    elif item.filing_sentiment == "risk":
        score += 12
        reasons.append("fresh SEC risk filing needs review")

    if "bankrupt" in kind or "delist" in kind:
        score += 45
        reasons.append("listing or solvency risk can break price discovery")
        hard_veto = True
    if "going concern" in kind:
        score += 35
        reasons.append("auditor warns the treasury may not survive")
        hard_veto = True
    if "insider sale" in kind:
        score += 8
        reasons.append("insider sale needs plan and stake context")
    return score, reasons, hard_veto


def assess_risk(item: RiskInput) -> RiskAssessment:
    """Return an independent rug score and a rules-based trade state."""

    points = 0.0
    reasons: list[str] = []
    hard_veto = False
    crash_candidate = item.drawdown_90d_pct >= 60 or item.drawdown_52w_pct >= 60

    if item.active_halt:
        points += 50
        reasons.append("trading is halted; price discovery is broken")
        hard_veto = True
    if item.going_concern:
        points += 35
        reasons.append("going-concern warning: treasury survival is in doubt")
        hard_veto = True

    filing_points, filing_reasons, filing_veto = _filing_flags(item)
    points += filing_points
    reasons.extend(filing_reasons)
    hard_veto = hard_veto or filing_veto

    if item.price < 1:
        points += 10
        reasons.append("sub-$1 tokenomics")
    if item.dollar_volume < 250_000:
        points += 14
        reasons.append("thin liquidity can turn you into exit liquidity")
    if 0 < item.recent_dollar_volume < 75_000:
        points += 8
        reasons.append("recent liquidity is thin")
    if item.stale_minutes > 15:
        points += 12
        reasons.append("quote is stale")

    if crash_candidate:
        points += 12
        reasons.append("down 60%+: bounce is not a repaired thesis")
        if item.rebound_from_20d_low_pct >= 10 and (
            item.vwap_position_pct <= 0 or item.momentum_15m_pct < 2
        ):
            points += 10
            reasons.append("dead-cat bounce has not reclaimed structure")
    elif item.drawdown_52w_pct >= 40:
        points += 6
        reasons.append("deep drawdown still carries trapped supply")

    if item.change_pct <= -20:
        points += 10
        reasons.append("live knife: price is still repricing lower")
    if item.vwap_position_pct <= -1:
        points += 7
        reasons.append("below VWAP; sellers still control the tape")
    if item.pullback_from_high_pct >= 6:
        points += 6
        reasons.append("failed to hold the session move")
    if item.momentum_15m_pct >= 12:
        points += 7
        reasons.append("parabolic move raises late-entry risk")

    if item.reverse_split_count_1y:
        points += min(24, 12 * item.reverse_split_count_1y)
        reasons.append("ticker reset: reverse-split cycle detected")
    if item.shares_growth_pct is not None:
        if item.shares_growth_pct >= 50:
            points += 30
            reasons.append(f"share supply grew {item.shares_growth_pct:.0f}%")
        elif item.shares_growth_pct >= 20:
            points += 20
            reasons.append(f"share supply grew {item.shares_growth_pct:.0f}%")
        elif item.shares_growth_pct >= 10:
            points += 10
            reasons.append(f"share supply grew {item.shares_growth_pct:.0f}%")
    if item.cash_runway_months is not None:
        if item.cash_runway_months < 3:
            points += 28
            reasons.append("treasury runway is under 3 months; raise risk is critical")
        elif item.cash_runway_months < 6:
            points += 18
            reasons.append("treasury runway is under 6 months")
        elif item.cash_runway_months < 12:
            points += 8
            reasons.append("treasury runway is under 12 months")
    if item.current_ratio is not None and item.current_ratio < 0.7:
        points += 12
        reasons.append("short-term liabilities outweigh liquid assets")
    if item.debt_to_cash is not None and item.debt_to_cash > 3:
        points += 10
        reasons.append("debt is more than 3× reported cash")
    if item.issuer_data_available is False:
        points += 8
        reasons.append("treasury and share-supply facts are still unknown")

    if (
        item.institutional_change_pct is not None
        and item.institutional_change_pct <= -15
    ):
        points += 8
        reasons.append("reported institutional ownership fell sharply")
    if (
        item.institutional_data_age_days is not None
        and item.institutional_data_age_days > 120
    ):
        points += 4
        reasons.append("institutional ownership data is stale")
    if item.beneficial_ownership_pct is not None:
        if item.beneficial_ownership_pct >= 50:
            points += 8
            reasons.append("one reported holder controls most of the supply")
        elif item.beneficial_ownership_pct >= 25:
            points += 4
            reasons.append("reported beneficial ownership is concentrated")

    rug_score = round(_clamp(points), 1)
    if rug_score >= 75:
        rug_level = "CRITICAL"
    elif rug_score >= 50:
        rug_level = "HIGH"
    elif rug_score >= 25:
        rug_level = "GUARDED"
    else:
        rug_level = "LOW"

    structure_confirmed = (
        item.vwap_position_pct > 0
        and item.momentum_15m_pct >= 2
        and item.momentum_5m_pct >= 0
        and item.close_location >= 0.55
    )
    previous = (item.previous_trade_state or "").upper()
    invalidated = (
        rug_score >= 65
        or item.vwap_position_pct <= -1
        or item.momentum_15m_pct <= -3
        or hard_veto
    )

    if previous in {"TRIGGERED", "MANAGE"}:
        if invalidated:
            trade_state = "EXIT"
            state_reason = "The prior setup is invalidated by risk or broken price structure."
        else:
            trade_state = "MANAGE"
            state_reason = (
                "The prior trigger remains valid; watch the reclaimed level and new filings."
            )
    elif hard_veto or rug_score >= 75:
        trade_state = "AVOID"
        state_reason = "Hard risk blocks this setup even if it can still pump."
    elif crash_candidate:
        if structure_confirmed and rug_score < 50:
            trade_state = "TRIGGERED"
            state_reason = "The crash setup reclaimed VWAP with momentum and acceptable rug risk."
        elif (
            rug_score < 65
            and item.vwap_position_pct >= -0.5
            and item.momentum_15m_pct > 0
        ):
            trade_state = "ARMED"
            state_reason = "The bounce is improving, but the reclaim still needs confirmation."
        else:
            trade_state = "WATCH"
            state_reason = "Crash candidate only; do not confuse a bounce with a repaired trend."
    elif structure_confirmed and item.setup_score >= 58 and rug_score < 50:
        trade_state = "TRIGGERED"
        state_reason = "Setup conditions are confirmed and rug risk is below the block level."
    elif (
        item.setup_score >= 45
        and rug_score < 65
        and (item.vwap_position_pct >= 0 or item.momentum_5m_pct > 0)
    ):
        trade_state = "ARMED"
        state_reason = "The setup is close, but one or more confirmation checks are missing."
    else:
        trade_state = "WATCH"
        state_reason = "No entry state is confirmed."

    return RiskAssessment(
        rug_score=rug_score,
        rug_level=rug_level,
        trade_state=trade_state,
        state_reason=state_reason,
        hard_veto=hard_veto,
        crash_candidate=crash_candidate,
        risk_reasons=list(dict.fromkeys(reasons)),
    )
