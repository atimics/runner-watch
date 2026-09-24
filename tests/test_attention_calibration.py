from datetime import UTC, datetime, timedelta

import pytest

from runner_web import attention
from runner_web import attention_calibration as calibration
from runner_web.attention_model import artifact
from runner_web.attention_trial import CONTRACT

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def cohort():
    runs, rows = [], []
    for day in range(10):
        date = (NOW - timedelta(days=20 - day)).date().isoformat()
        rid = f"run-{day}"
        runs.append({"id": rid, "model_sha256": artifact()["sha256"],
                     "policy": attention.POLICY_VERSION,
                     "contract_json": calibration._canonical(CONTRACT).decode(),
                     "day": date, "status": "recorded", "expected_rows": 3,
                     "deadline_at": (NOW - timedelta(days=20 - day)).isoformat(),
                     "evidence_as_of": date, "saved_at": date})
        for index, p in enumerate((.15, .45, .85)):
            rows.append({"run_id": rid, "ticker": f"T{index}",
                         "snapshot_id": f"snapshot-{day}-{index}",
                         "reason": "learned", "probability": p,
                         "outcome_status": "resolved", "target": index % 2,
                         "outcome_json": "{}"})
    return runs, rows


def test_freeze_uses_exact_complete_cohort_and_retains_unknown_slots():
    runs, rows = cohort()
    rows[0]["outcome_status"] = "unknown"
    rows[0]["target"] = None
    result = calibration.freeze_from_receipts(runs, rows, now=NOW)
    assert result["state"] == "frozen_candidate"
    assert result["selected_predictions"] == 30
    assert result["unknown_predictions"] == 1
    assert result["resolved_learned_predictions"] == 29
    assert result["promotion_ready"] is False
    assert all(a["value"] <= b["value"] for a, b in zip(
        result["bins"], result["bins"][1:], strict=False))
    future = dict(rows[-1], probability=.01, target=0, ticker="FUTURE")
    future_run = dict(runs[-1], id="future", day=(NOW + timedelta(days=1)).date().isoformat(),
                      deadline_at=(NOW + timedelta(days=2)).isoformat(), expected_rows=1)
    future["run_id"] = "future"
    assert calibration.freeze_from_receipts(runs + [future_run], rows + [future], now=NOW) == result


def test_missing_or_pending_row_blocks_a_day_and_prevents_fit():
    runs, rows = cohort()
    rows.pop()
    result = calibration.freeze_from_receipts(runs, rows, now=NOW)
    assert result["state"] == "collecting"
    assert result["completed_sessions"] == 9
    assert result["blocked_days"][-1]["reason"] == "incomplete_receipts"
    runs, rows = cohort()
    rows[-1]["outcome_status"] = "pending"
    assert calibration.freeze_from_receipts(runs, rows, now=NOW)["state"] == "collecting"


def test_bad_probability_rejected_even_with_ten_complete_sessions():
    runs, rows = cohort()
    rows[0]["probability"] = float("nan")
    with pytest.raises(ValueError, match="probability"):
        calibration.freeze_from_receipts(runs, rows, now=NOW)
