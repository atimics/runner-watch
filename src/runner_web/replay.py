"""Bounded evidence reconstruction and evaluation of the exact served policy.

Mutable source approvals, model activation and community records do not yet have
complete revision ledgers. The loader excludes later observations/revisions and
reports these limitations; it is not a claim of exact historical reconstruction.
Training uses frozen fold membership with label-interval purging at BOTH
boundaries, with the resulting counts passed explicitly to the Rust trainer.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.attention import attention_order
from runner_web.labels import BAR_TOLERANCE, BARRIER_HORIZON

# Material activity: the price did something inside the horizon, either way.
MATERIAL_OUTCOMES = {"up", "down"}


def point_in_time_inputs(
    at: datetime | None = None, *, ticker: str | None = None
) -> dict[str, Any]:
    """Bounded reconstruction; inspect replay_status before claiming exact replay.

    Mutable approval/model/community states have no revision ledger yet. The
    loader excludes later revisions and reports that coverage limitation.
    """

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


def evaluate_served_policy(rows: list[dict[str, Any]], *, k: int = 10) -> dict[str, Any]:
    """Evaluate the actual top-k without replacing unresolved selected observations.

    ``served_precision`` is conditional on resolution. The lower/upper bounds and
    coverage describe the original review budget, including missing outcomes.
    Call once per scan; pooling different scan universes is a different policy.
    """

    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("k must be a positive integer")
    runs = {row["scan_run_id"] for row in rows if row.get("scan_run_id")}
    if len(runs) > 1:
        raise ValueError("Evaluate each scan separately")

    def resolved(row: dict[str, Any]) -> bool:
        return (
            row.get("barrier_label") in {"up", "down", "timeout"}
            and not row.get("barrier_ambiguous")
            and (row.get("barrier_resolution") or "resolved") == "resolved"
        )

    usable = [row for row in rows if resolved(row)]
    top = sorted(rows, key=attention_order)[:k]
    known_top = [row for row in top if resolved(row)]
    hits = sum(row["barrier_label"] in MATERIAL_OUTCOMES for row in known_top)
    unknown = len(top) - len(known_top)
    payoff = sorted(
        rows,
        key=lambda row: (
            _number(row.get("model_payoff_bp")) is None,
            -(_number(row.get("model_payoff_bp")) or 0),
            str(row.get("ticker") or ""),
        ),
    )[:k]
    payoff_known = [row for row in payoff if resolved(row)]
    blocked = sum(
        bool(row.get("blocked") or (row.get("eligibility") or {}).get("blocked")) for row in top
    )
    return {
        "rows": len(rows),
        "resolved_rows": len(usable),
        "k": len(top),
        "base_rate": (
            round(sum(row["barrier_label"] in MATERIAL_OUTCOMES for row in usable) / len(usable), 4)
            if usable
            else None
        ),
        "served_precision": round(hits / len(known_top), 4) if known_top else None,
        "payoff_precision": (
            round(
                sum(row["barrier_label"] in MATERIAL_OUTCOMES for row in payoff_known)
                / len(payoff_known),
                4,
            )
            if payoff_known
            else None
        ),
        "selected_resolved": len(known_top),
        "selected_unresolved": unknown,
        "coverage_at_k": round(len(known_top) / len(top), 4) if top else None,
        "precision_lower_bound": round(hits / len(top), 4) if top else None,
        "precision_upper_bound": round((hits + unknown) / len(top), 4) if top else None,
        "blocked_in_top": blocked,
        "blocked_share": round(blocked / len(top), 4) if top else None,
    }


def purged_chronological_split(
    groups: list[list[dict[str, Any]]],
    *,
    horizon: timedelta = BARRIER_HORIZON,
) -> dict[str, Any]:
    """Freeze 80/10/10 membership, then purge both label-overlap boundaries.

    Never concatenate these partitions and recompute fractional boundaries. The
    returned counts are passed explicitly across the Python/Rust protocol.
    Timestamps are required; a missing clock must not silently disable purging.
    """

    if len(groups) < 3:
        raise ValueError("At least three timestamped groups are required")
    if horizon <= timedelta(0):
        raise ValueError("Outcome horizon must be positive")
    starts: list[datetime] = []
    ends: list[datetime] = []
    for group in groups:
        if not group:
            raise ValueError("Empty training group")
        row_starts, row_ends = [], []
        for row in group:
            start = _moment(row.get("run_captured_at") or row.get("captured_at"))
            if start is None:
                raise ValueError("Training group has no valid decision timestamp")
            end = (
                _moment(row.get("label_end_at"))
                if row.get("label_end_at") is not None
                else start + horizon + BAR_TOLERANCE
            )
            if end is None or end < start:
                raise ValueError("Invalid label interval")
            row_starts.append(start)
            row_ends.append(end)
        starts.append(min(row_starts))
        ends.append(max(row_ends))
    if starts != sorted(starts):
        raise ValueError("Training groups must be ordered chronologically")
    held = min(max(2, (len(groups) + 2) // 5), len(groups) - 1)
    train_end = len(groups) - held
    validation_end = train_end + held // 2
    validation_start, test_start = starts[train_end], starts[validation_end]
    train, validation, purged = [], [], []
    for i in range(train_end):
        if ends[i] >= validation_start:
            purged.append(_run_id(groups[i]))
        else:
            train.append(groups[i])
    for i in range(train_end, validation_end):
        if ends[i] >= test_start:
            purged.append(_run_id(groups[i]))
        else:
            validation.append(groups[i])
    test = groups[validation_end:]
    receipt = {
        "policy": "fixed-membership-purged-v2",
        "original_groups": len(groups),
        "train_groups": len(train),
        "validation_groups": len(validation),
        "test_groups": len(test),
        "purged_groups": len(purged),
        "purged_runs": purged,
        "validation_start": validation_start.isoformat(),
        "test_start": test_start.isoformat(),
        "horizon_minutes": horizon.total_seconds() / 60,
        "tolerance_minutes": BAR_TOLERANCE.total_seconds() / 60,
    }
    return {"train": train, "validation": validation, "test": test, "receipt": receipt}


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
        moment = _moment(row.get("run_captured_at") or row.get("captured_at"))
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
    return float(value) if math.isfinite(value) else None
