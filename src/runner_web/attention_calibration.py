"""Fit a frozen, monotone attention calibration map from trial receipts.

This command reads durable predictions only. It does not change the public board.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from runner_web import attention
from runner_web.attention_model import artifact
from runner_web.attention_trial import CONTRACT
from runner_web.db import connection


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def fit_map(rows: list[dict]) -> list[dict]:
    """Fixed fifths, binomial smoothed, pooled-adjacent-violators calibration."""
    bins = [{"low": i / 5, "high": (i + 1) / 5, "count": 0, "hits": 0}
            for i in range(5)]
    for row in rows:
        p, target = row["probability"], row["target"]
        if not isinstance(p, (float, int)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError("invalid frozen probability")
        if target not in (0, 1) or isinstance(target, bool):
            raise ValueError("invalid resolved target")
        entry = bins[min(4, int(p * 5))]
        entry["count"] += 1
        entry["hits"] += target
    # Empty bins do not invent a probability. The nearest populated pooled
    # interval supplies a lookup for them at use time.
    groups: list[dict] = []
    for entry in bins:
        if entry["count"] == 0:
            continue
        groups.append(dict(entry))
        while len(groups) > 1:
            a, b = groups[-2:]
            pa = (a["hits"] + 1) / (a["count"] + 2)
            pb = (b["hits"] + 1) / (b["count"] + 2)
            if pa <= pb:
                break
            groups[-2:] = [{"low": a["low"], "high": b["high"],
                            "count": a["count"] + b["count"],
                            "hits": a["hits"] + b["hits"]}]
    return [{**group, "value": (group["hits"] + 1) / (group["count"] + 2)}
            for group in groups]


def freeze_from_receipts(runs: list[dict], predictions: list[dict], *, now: datetime) -> dict:
    model = artifact()
    run_rows: dict[str, list[dict]] = defaultdict(list)
    for row in predictions:
        run_rows[row["run_id"]].append(row)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        if (run["model_sha256"] == model["sha256"] and
                run["policy"] == attention.POLICY_VERSION and
                run["status"] != "outside_window"):
            by_day[run["day"]].append(run)
    completed = []
    blockers = []
    for day in sorted(by_day):
        day_runs = by_day[day]
        if any(r["contract_json"] != _canonical(CONTRACT).decode() for r in day_runs):
            blockers.append({"day": day, "reason": "contract_mismatch"})
            continue
        if any(r["status"] != "recorded" or not r["deadline_at"] or
               datetime.fromisoformat(r["deadline_at"]) > now or
               len(run_rows[r["id"]]) != r["expected_rows"] or
               any(p["outcome_status"] not in ("resolved", "unknown")
                   for p in run_rows[r["id"]]) for r in day_runs):
            blockers.append({"day": day, "reason": "incomplete_receipts"})
            continue
        completed.append(day)
    selected_days = completed[:10]
    if len(selected_days) < 10:
        return {"schema": "attention.calibration_readiness.v1",
                "state": "collecting", "completed_sessions": len(completed),
                "sessions_remaining": 10 - len(selected_days), "blocked_days": blockers}
    cohort = sorted((p for day in selected_days for r in by_day[day]
                     for p in run_rows[r["id"]]),
                    key=lambda p: (p["run_id"], p["ticker"]))
    resolved = [p for p in cohort if p["outcome_status"] == "resolved"
                and p["reason"] == "learned"]
    if not resolved:
        raise ValueError("no resolved learned predictions in calibration cohort")
    for p in resolved:
        probability = p["probability"]
        if (not isinstance(probability, (float, int)) or
                not math.isfinite(probability) or not 0 <= probability <= 1):
            raise ValueError("invalid frozen probability")
    source = [{"run_id": p["run_id"], "ticker": p["ticker"],
               "snapshot_id": p["snapshot_id"],
               "reason": p["reason"], "outcome_status": p["outcome_status"],
               "probability": p["probability"], "target": p["target"],
               "outcome_json": p["outcome_json"]} for p in cohort]
    run_evidence = [{"id": r["id"], "evidence_as_of": r["evidence_as_of"],
                     "saved_at": r["saved_at"], "expected_rows": r["expected_rows"]}
                    for day in selected_days for r in by_day[day]]
    source_digest = hashlib.sha256(_canonical({
        "runs": run_evidence, "predictions": source,
    })).hexdigest()
    bins = fit_map(resolved)

    def mapped(probability: float) -> float:
        return min(bins, key=lambda b: max(b["low"] - probability, 0,
                                            probability - b["high"]))["value"]

    raw_brier = sum((p["probability"] - p["target"]) ** 2 for p in resolved) / len(resolved)
    fitted_brier = sum((mapped(p["probability"]) - p["target"]) ** 2
                       for p in resolved) / len(resolved)
    return {"schema": "attention.calibration_map.v1", "state": "frozen_candidate",
            "model_sha256": model["sha256"], "baseline_policy": attention.POLICY_VERSION,
            "contract_sha256": hashlib.sha256(_canonical(CONTRACT)).hexdigest(),
            "source_sha256": source_digest,
            "cutoff_day": selected_days[-1], "calibration_days": selected_days,
            "selected_predictions": len(cohort), "unknown_predictions": sum(
                p["outcome_status"] == "unknown" for p in cohort),
            "fallback_predictions": sum(p["reason"] != "learned" for p in cohort),
            "resolved_learned_predictions": len(resolved),
            "bins": bins, "calibration_only_raw_brier": raw_brier,
            "calibration_only_fitted_brier": fitted_brier,
            "evaluation_policy": "future_sessions_only",
            "promotion_ready": False}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write candidate artifact after ten complete sessions")
    args = parser.parse_args()
    with connection() as db:
        runs = [dict(r) for r in db.execute("SELECT * FROM attention_trial_runs")]
        predictions = [dict(p) for p in db.execute(
            "SELECT run_id,ticker,snapshot_id,reason,probability,outcome_status,target,"
            "outcome_json "
            "FROM attention_trial_predictions")]
    result = freeze_from_receipts(runs, predictions, now=datetime.now(UTC))
    if args.out and result["state"] == "frozen_candidate":
        Path(args.out).write_bytes(_canonical(result) + b"\n")
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
