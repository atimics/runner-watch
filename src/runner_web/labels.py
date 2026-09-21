"""The outcome contract: what a label means, and which version produced it.

The barriers and horizon used to live as loose constants next to the labeller.
Naming them here lets every prediction record the contract it was made under, so
changing the barriers later is a new version rather than a silent
reinterpretation of the same column.

``resolved`` means the window answered the question: one barrier was touched
first, or neither was touched within the horizon. ``ambiguous`` means a single
bar touched both, so the order is unknowable from the data and the label is the
pessimistic reading rather than a fact. Ambiguous rows are held out of training.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

UPPER_BARRIER_PCT = 8.0
LOWER_BARRIER_PCT = 4.0
BARRIER_HORIZON = timedelta(minutes=60)
BAR_TOLERANCE = timedelta(minutes=10)

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"

POLICY_VERSION = (
    f"barriers.v1:+{UPPER_BARRIER_PCT:g}%/-{LOWER_BARRIER_PCT:g}%"
    f"/{int(BARRIER_HORIZON.total_seconds() // 60)}m"
)


def barrier_contract() -> dict[str, Any]:
    """The label contract, as recorded with each prediction."""

    return {
        "policy": POLICY_VERSION,
        "upper_pct": UPPER_BARRIER_PCT,
        "lower_pct": LOWER_BARRIER_PCT,
        "horizon_minutes": int(BARRIER_HORIZON.total_seconds() // 60),
    }