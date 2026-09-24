"""Two questions the public list used to answer with one number.

"Is this worth looking at?" and "may I act on it?" are different questions with
legitimately different answers. A company announcing a solvency problem can
deserve the highest attention on the board *and* an unequivocal block, and
subtracting the risk from the attention made that impossible to express.

This module keeps them apart:

* ``attention_score`` orders the list. It reads a direction-neutral activity index plus
  material evidence -- a filing, news and social activity. A risk
  filing raises attention, because something important is happening; how
  bearish it is belongs to the forecast and the block, not to notice.
* ``eligibility`` is deterministic policy. It never guesses: a halt, an exit
  state or missing prices produce a blocked or unknown state with a reason code,
  and no score can override it.

Neither function is a probability. The outcome forecast keeps its own contract.
"""

from __future__ import annotations

import json
import math
from typing import Any

from runner_web.labels import barrier_contract

# Attention is clamped to the same 0-100 window the list has always used.
ATTENTION_MAX = 100.0
# Evidence that merely happened is worth this much at most, whatever its sign.
EVENT_MAX = 12.0
NEWS_MAX = 6.0
SOCIAL_MAX = 8.0
CLUSTER_MAX = 8.0
COMMUNITY_MAX = 8.0

ELIGIBLE = "eligible"
BLOCKED = "blocked"
UNKNOWN = "unknown"

BLOCKING_STATES = {"EXIT", "AVOID"}
BLOCKING_RUG_LEVELS = {"high", "critical"}
BLOCKING_RUG_SCORE = 50.0
POLICY_VERSION = "attention-activity-v3"
MAX_QUOTE_AGE_MINUTES = 15.0
MAX_ASSESSMENT_AGE_MINUTES = 60.0


def _clamp(value: float, low: float = 0.0, high: float = ATTENTION_MAX) -> float:
    number = finite_number(value)
    return low if number is None else max(low, min(high, number))


def event_attention(catalyst_score: float) -> float:
    """How much a filing moves attention: its size, not its direction.

    A risk filing is material news; a positive filing is material news. Both are
    capped the same way so the list can surface either.
    """

    return min(EVENT_MAX, _clamp(catalyst_score) * 0.12)


def community_attention(call_count: int) -> float:
    """Engagement is descriptive, not independent evidence of market activity.

    Both Calls and generated comments are affected by the board's own exposure.
    Keep the compatibility function while refusing to reward that feedback loop.
    """

    return 0.0


def cluster_attention(cluster_value: Any, stock_value: Any) -> float:
    """Capped portfolio-scale signal, reduced for a small holding in its cluster."""
    total = finite_number(cluster_value)
    holding = finite_number(stock_value)
    if total is None or holding is None or total <= 0 or holding <= 0:
        return 0.0
    scale = min(CLUSTER_MAX, 2.0 * math.log10(1.0 + total / 10_000.0))
    return min(CLUSTER_MAX, scale * math.sqrt(min(1.0, holding / total)))


def attention_score(
    *,
    signal: float,
    event: float = 0.0,
    news: float = 0.0,
    social: float = 0.0,
    cluster: float = 0.0,
    community: float = 0.0,
) -> float:
    """The ordering number: what deserves investigation now."""

    return round(
        _clamp(
            _clamp(signal)
            + _clamp(event, high=EVENT_MAX)
            + _clamp(news, high=NEWS_MAX)
            + _clamp(social, high=SOCIAL_MAX)
            + _clamp(cluster, high=CLUSTER_MAX)
        ),
        2,
    )


def eligibility(
    *,
    active_halt: bool = False,
    trade_state: str = "",
    rug_score: float | None = None,
    rug_level: str = "",
    has_price: bool = True,
    hard_veto: bool = False,
    stale_minutes: float | None = None,
    assessment_age_minutes: float | None = None,
    require_complete: bool = False,
) -> dict[str, Any]:
    """What the application permits, with reasons a reader can audit."""

    reasons: list[dict[str, str]] = []
    state = ELIGIBLE
    if not has_price:
        state = UNKNOWN
        reasons.append({"code": "no_price", "label": "No current price"})
    age = finite_number(stale_minutes)
    if age is not None and (age < 0 or age > MAX_QUOTE_AGE_MINUTES):
        state = UNKNOWN
        reasons.append({"code": "stale_quote", "label": "Quote is not current"})
    elif require_complete and age is None:
        state = UNKNOWN
        reasons.append({"code": "quote_age_unknown", "label": "Quote age is unknown"})
    assessment_age = finite_number(assessment_age_minutes)
    if assessment_age is not None and (
        assessment_age < 0 or assessment_age > MAX_ASSESSMENT_AGE_MINUTES
    ):
        state = UNKNOWN
        reasons.append({"code": "stale_assessment", "label": "Scan assessment is not current"})
    if require_complete and trade_state.strip().upper() not in {
        "WATCH",
        "ARMED",
        "TRIGGERED",
        "MANAGE",
        "AVOID",
        "EXIT",
    }:
        state = UNKNOWN
        reasons.append({"code": "risk_unassessed", "label": "Risk assessment is unavailable"})
    risk_value = finite_number(rug_score)
    if require_complete and (risk_value is None or not 0 <= risk_value <= 100):
        state = UNKNOWN
        reasons.append({"code": "risk_unknown", "label": "Risk score is unavailable"})
    if hard_veto:
        state = BLOCKED
        reasons.append({"code": "hard_veto", "label": "Deterministic risk veto"})
    if active_halt:
        state = BLOCKED
        reasons.append({"code": "trading_halt", "label": "Trading halt"})
    upper = trade_state.strip().upper()
    if upper in BLOCKING_STATES:
        state = BLOCKED
        reasons.append({"code": f"state_{upper.lower()}", "label": f"Scanner state {upper}"})
    if rug_level.strip().lower() in BLOCKING_RUG_LEVELS:
        state = BLOCKED
        reasons.append({"code": "rug_risk", "label": "Risk factors triggered a block"})
    elif risk_value is not None and risk_value >= BLOCKING_RUG_SCORE:
        state = BLOCKED
        reasons.append({"code": "rug_score", "label": "Reported risk score at veto level"})
    return {
        "state": state,
        "reasons": reasons,
        "blocked": state == BLOCKED,
        "eligible": state == ELIGIBLE,
    }


def risk_note(eligibility_state: dict[str, Any]) -> str:
    """One line for a reader, or empty when nothing is blocked or unknown."""

    if eligibility_state.get("state") == ELIGIBLE:
        return ""
    labels = [str(reason.get("label") or "") for reason in eligibility_state.get("reasons") or []]
    return " · ".join(label for label in labels if label)


def finite_number(value: Any) -> float | None:
    """Do not turn unknowns, booleans or non-finite provider values into facts."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def market_activity(snapshot: dict[str, Any]) -> dict[str, Any]:
    """An uncalibrated, direction-neutral activity index, never a probability.

    Maxima avoid rewarding overlapping momentum/volume windows twice. Constants
    are declared heuristic policy, not fitted coefficients or claims of alpha.
    Missing feature coverage is returned explicitly alongside the zero contribution.
    """

    fields = (
        "relative_volume",
        "recent_relative_volume",
        "momentum_5m_pct",
        "momentum_15m_pct",
        "change_pct",
    )
    values = {key: finite_number(snapshot.get(key)) for key in fields}
    missing = [key for key, value in values.items() if value is None]
    rvol = max(1.0, values["relative_volume"] or 0, values["recent_relative_volume"] or 0)
    volume = min(35.0, 8.0 * math.log2(rvol))
    momentum = min(
        30.0,
        max(abs(values["momentum_5m_pct"] or 0) * 6.0, abs(values["momentum_15m_pct"] or 0) * 2.0),
    )
    move = min(15.0, abs(values["change_pct"] or 0))
    age = finite_number(snapshot.get("stale_minutes"))
    # Unknown or future quote time cannot be represented as fresh market activity.
    freshness = 0.0 if age is None or age < 0 else math.exp(-max(0.0, age - 5.0) / 15.0)
    return {
        "value": round((volume + momentum + move) * freshness, 2),
        "status": "unavailable"
        if len(missing) == len(fields) or freshness == 0
        else "partial"
        if missing
        else "available",
        "missing": missing,
        "freshness": round(freshness, 6),
        "volume": round(volume, 2),
        "momentum": round(momentum, 2),
        "move": round(move, 2),
    }


def forecast_facts(prediction: dict[str, Any] | None) -> dict[str, Any] | None:
    """Preserve all three forecast probabilities in their original measurement units."""

    if not prediction:
        return None
    contract = barrier_contract()
    saved_contract = prediction.get("label_contract")
    if saved_contract is not None:
        try:
            parsed = (
                json.loads(saved_contract) if isinstance(saved_contract, str) else saved_contract
            )
        except (TypeError, ValueError):
            return None
        if parsed != contract:
            return None
    probabilities = {
        key: finite_number(prediction.get(f"probability_{key}"))
        for key in ("down", "timeout", "up")
    }
    if any(value is None or not 0 <= value <= 1 for value in probabilities.values()):
        return None
    if abs(sum(probabilities.values()) - 1.0) > 1e-6:
        return None
    return {
        "probability_up": probabilities["up"],
        "probability_down": probabilities["down"],
        "probability_timeout": probabilities["timeout"],
        "activity_probability": 1.0 - probabilities["timeout"],
        "assumed_barrier_payoff_pct": finite_number(prediction.get("expected_return_pct")),
        "model_id": prediction.get("model_id"),
        "model_rank": prediction.get("rank"),
        "as_of": prediction.get("created_at"),
        "contract": "+8% before -4%, or -4% before +8%, within 60 minutes",
        "label_contract": contract,
        "contract_basis": "recorded" if saved_contract is not None else "legacy_assumed_v1",
        "payoff_note": "Assumed barrier exits, before costs; not a terminal-return forecast",
        "downside_note": "Lower-first probability is not total drawdown probability",
    }


def attention_order(row: dict[str, Any]) -> tuple[Any, ...]:
    """One deterministic ordering used by the public board and replay evaluator."""

    score = finite_number(row.get("attention_score", row.get("attention", row.get("score"))))
    rank = finite_number(row.get("baseline_rank"))
    return (
        -int(bool(row.get("attention_urgent"))),
        -(score or 0.0),
        rank if rank is not None and rank > 0 else 1_000_000,
        str(row.get("ticker") or row.get("snapshot_id") or ""),
    )
