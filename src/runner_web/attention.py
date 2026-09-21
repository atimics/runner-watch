"""Two questions the public list used to answer with one number.

"Is this worth looking at?" and "may I act on it?" are different questions with
legitimately different answers. A company announcing a solvency problem can
deserve the highest attention on the board *and* an unequivocal block, and
subtracting the risk from the attention made that impossible to express.

This module keeps them apart:

* ``attention_score`` orders the list. It reads the scanner or model signal plus
  material evidence -- a filing, news, social activity, active Callers. A risk
  filing raises attention, because something important is happening; how
  bearish it is belongs to the forecast and the block, not to notice.
* ``eligibility`` is deterministic policy. It never guesses: a halt, an exit
  state or missing prices produce a blocked or unknown state with a reason code,
  and no score can override it.

Neither function is a probability. The outcome forecast keeps its own contract.
"""

from __future__ import annotations

import math
from typing import Any

# Attention is clamped to the same 0-100 window the list has always used.
ATTENTION_MAX = 100.0
# Evidence that merely happened is worth this much at most, whatever its sign.
EVENT_MAX = 12.0
NEWS_MAX = 6.0
SOCIAL_MAX = 8.0
COMMUNITY_MAX = 8.0

ELIGIBLE = "eligible"
BLOCKED = "blocked"
UNKNOWN = "unknown"

BLOCKING_STATES = {"EXIT", "AVOID"}
BLOCKING_RUG_LEVELS = {"high", "critical"}
BLOCKING_RUG_SCORE = 90.0


def _clamp(value: float, low: float = 0.0, high: float = ATTENTION_MAX) -> float:
    return max(low, min(high, value))


def event_attention(catalyst_score: float) -> float:
    """How much a filing moves attention: its size, not its direction.

    A risk filing is material news; a positive filing is material news. Both are
    capped the same way so the list can surface either.
    """

    return min(EVENT_MAX, max(0.0, catalyst_score) * 0.12)


def community_attention(call_count: int) -> float:
    """Active Callers only.

    Lifetime comment count used to reach the cap on its own, which meant a
    well-placed row could keep itself placed through the comments it attracted.
    Engagement is logged, not scored.
    """

    return min(COMMUNITY_MAX, math.log2(max(0, call_count) + 1) * 2.0)


def attention_score(
    *,
    signal: float,
    event: float = 0.0,
    news: float = 0.0,
    social: float = 0.0,
    community: float = 0.0,
) -> float:
    """The ordering number: what deserves investigation now."""

    return round(_clamp(signal + event + news + social + community), 2)


def eligibility(
    *,
    active_halt: bool = False,
    trade_state: str = "",
    rug_score: float | None = None,
    rug_level: str = "",
    has_price: bool = True,
) -> dict[str, Any]:
    """What the application permits, with reasons a reader can audit."""

    reasons: list[dict[str, str]] = []
    state = ELIGIBLE
    if not has_price:
        state = UNKNOWN
        reasons.append({"code": "no_price", "label": "No current price"})
    if active_halt:
        state = BLOCKED
        reasons.append({"code": "trading_halt", "label": "Trading halt"})
    upper = trade_state.strip().upper()
    if upper in BLOCKING_STATES:
        state = BLOCKED
        reasons.append({"code": f"state_{upper.lower()}", "label": f"Scanner state {upper}"})
    if rug_level.strip().lower() in BLOCKING_RUG_LEVELS:
        state = BLOCKED
        reasons.append({"code": "rug_risk", "label": "Reported risk level high"})
    elif rug_score is not None and rug_score >= BLOCKING_RUG_SCORE:
        state = BLOCKED
        reasons.append({"code": "rug_score", "label": "Reported risk score at veto level"})
    return {"state": state, "reasons": reasons, "blocked": state == BLOCKED}


def risk_note(eligibility_state: dict[str, Any]) -> str:
    """One line for a reader, or empty when nothing is blocked or unknown."""

    if eligibility_state.get("state") == ELIGIBLE:
        return ""
    labels = [str(reason.get("label") or "") for reason in eligibility_state.get("reasons") or []]
    return " · ".join(label for label in labels if label)