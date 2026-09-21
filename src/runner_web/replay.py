"""Replay the board the way it was known, and judge the policy that serves it.

Two defects made historical evaluation untrustworthy:

* the scoring loader had lower time bounds but no upper ones, so replaying a past
  moment still read filings, events, comments and the *current* model from after
  it. ``point_in_time_inputs`` is the named entry point for a moment, and the
  loader it calls now bounds every read by ``at``.
* training groups were split by position alone, so a training group whose outcome
  window had not finished when the next group was captured could teach the model
  the future. ``purge_boundary_groups`` drops those before the split.

``evaluate_served_policy`` closes the loop the audit opened: rank by what the
list actually shows (attention, with blocked names reported rather than hidden)
and score that ordering, instead of scoring a payoff ordering the reader never
sees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.labels import BAR_TOLERANCE, BARRIER_HORIZON

# Material activity: the price did something inside the horizon, either way.
MATERIAL_OUTCOMES = {"up", "down"}


def point_in_time_inputs(
    at: datetime | None = None, *, ticker: str | None = None
) -> dict[str, Any]:
    """Everything the scorer would have known at ``at``, and nothing later."""

    from runner_web.main import _pulse_scoring_inputs

    moment = at or datetime.now(UTC)
    return _pulse_scoring_inputs(ticker=ticker, at=moment)


def label_window_end(captured_at: Any, *, horizon: timedelta = BARRIER_HORIZON) -> datetime | None:
    """When a group's outcome is finally knowable."""

    moment = _moment(captured_at)
    return None if moment is None else moment + horizon + BAR_TOLERANCE


def purge_boundary_groups(
    groups: list[list[dict[str, Any]]],
    *,
    split: float = 0.8,
    horizon: timedelta = BARRIER_HORIZON,
) -> dict[str, Any]:
    """Drop training groups whose label window reaches into the held-out part.

    Groups arrive oldest first. The trainer splits them by position, so a group
    just before the boundary can share its outcome window with the first
    evaluation group; that leak is what this removes. Returns the kept groups and
    a note of what was purged, so the trainer's report can say so.
    """

    if len(groups) < 2:
        return {"groups": groups, "purged": 0, "purged_runs": []}
    boundary = max(1, min(len(groups) - 1, int(len(groups) * split)))
    first_held_out = _captured_at(groups[boundary])
    if first_held_out is None:
        return {"groups": groups, "purged": 0, "purged_runs": []}
    kept: list[list[dict[str, Any]]] = []
    purged_runs: list[str] = []
    for index, group in enumerate(groups):
        if index >= boundary:
            kept.append(group)
            continue
        window_end = label_window_end(_captured_at(group), horizon=horizon)
        if window_end is not None and window_end > first_held_out:
            purged_runs.append(_run_id(group))
            continue
        kept.append(group)
    return {"groups": kept, "purged": len(purged_runs), "purged_runs": purged_runs}


def evaluate_served_policy(
    rows: list[dict[str, Any]],
    *,
    k: int = 10,
) -> dict[str, Any]:
    """Score the ordering the reader actually sees.

    Each row needs ``attention`` (the served score), ``model_payoff_bp`` (the
    trainer's ordering) and ``barrier_label`` (what happened). Blocked rows are
    reported separately: they stay in the list, so they are part of the ordering,
    but a reader cannot act on them.
    """

    usable = [row for row in rows if row.get("barrier_label") in {"up", "down", "timeout"}]
    total = len(usable)
    if not total:
        return {"rows": 0, "k": k, "base_rate": None}
    activity = [row for row in usable if row.get("barrier_label") in MATERIAL_OUTCOMES]
    base_rate = len(activity) / total

    def precision(order: list[dict[str, Any]]) -> float | None:
        top = order[: max(1, min(k, len(order)))]
        if not top:
            return None
        hits = sum(1 for row in top if row.get("barrier_label") in MATERIAL_OUTCOMES)
        return round(hits / len(top), 4)

    served = sorted(usable, key=lambda row: _number(row.get("attention")) or 0.0, reverse=True)
    payoff = sorted(
        usable, key=lambda row: _number(row.get("model_payoff_bp")) or 0.0, reverse=True
    )
    top = served[: max(1, min(k, len(served)))]
    blocked = sum(1 for row in top if row.get("blocked"))
    return {
        "rows": total,
        "k": len(top),
        "base_rate": round(base_rate, 4),
        "served_precision": precision(served),
        "payoff_precision": precision(payoff),
        "blocked_in_top": blocked,
        "blocked_share": round(blocked / len(top), 4) if top else None,
    }


def _moment(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _captured_at(group: list[dict[str, Any]]) -> datetime | None:
    for row in group:
        moment = _moment(row.get("captured_at"))
        if moment is not None:
            return moment
    return None


def _run_id(group: list[dict[str, Any]]) -> str:
    for row in group:
        value = str(row.get("scan_run_id") or "")
        if value:
            return value
    return ""


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)