from __future__ import annotations

import math
from typing import Any

PROMOTION_POLICY = {
    "version": "ranker-promotion-v1",
    "minimum_groups_per_split": 16,
    "minimum_rows_per_split": 500,
    "minimum_selected_up_lift_ppm": 10_000,
    "minimum_return_lift_bp": 10,
    "maximum_up_brier_ppm": 250_000,
    "maximum_up_calibration_error_ppm": 100_000,
}


def promotion_status(metrics: Any) -> dict[str, Any]:
    reasons: list[str] = []
    if not isinstance(metrics, dict):
        metrics = {}
    for split in ("validation", "test"):
        values = metrics.get(split)
        if not isinstance(values, dict):
            reasons.append(f"{split}: missing evaluation")
            continue
        required = (
            "groups",
            "rows",
            "selected_up_rate_ppm",
            "baseline_selected_up_rate_ppm",
            "precision_at_5_ppm",
            "baseline_precision_at_5_ppm",
            "mean_selected_return_bp",
            "baseline_mean_selected_return_bp",
            "up_brier_ppm",
            "up_expected_calibration_error_ppm",
        )
        if any(
            not isinstance(values.get(key), (int, float))
            or isinstance(values[key], bool)
            or not math.isfinite(values[key])
            for key in required
        ):
            reasons.append(f"{split}: missing or invalid metrics")
            continue
        if any(not 0 <= values[key] <= 1_000_000 for key in required if key.endswith("_ppm")):
            reasons.append(f"{split}: invalid probability metrics")
            continue
        if values["groups"] < PROMOTION_POLICY["minimum_groups_per_split"]:
            reasons.append(f"{split}: more scan groups required")
        if values["rows"] < PROMOTION_POLICY["minimum_rows_per_split"]:
            reasons.append(f"{split}: more rows required")
        if (
            values["selected_up_rate_ppm"] - values["baseline_selected_up_rate_ppm"]
            < PROMOTION_POLICY["minimum_selected_up_lift_ppm"]
        ):
            reasons.append(f"{split}: selected outcomes need a larger gain over baseline")
        if values["precision_at_5_ppm"] < values["baseline_precision_at_5_ppm"]:
            reasons.append(f"{split}: top five outcomes trail baseline")
        if (
            values["mean_selected_return_bp"] <= 0
            or values["mean_selected_return_bp"] - values["baseline_mean_selected_return_bp"]
            < PROMOTION_POLICY["minimum_return_lift_bp"]
        ):
            reasons.append(f"{split}: selected returns need a larger gain over baseline")
        if values["up_brier_ppm"] > PROMOTION_POLICY["maximum_up_brier_ppm"]:
            reasons.append(f"{split}: probability error exceeds the limit")
        if (
            values["up_expected_calibration_error_ppm"]
            > PROMOTION_POLICY["maximum_up_calibration_error_ppm"]
        ):
            reasons.append(f"{split}: calibration error exceeds the limit")
    return {"eligible": not reasons, "reasons": reasons, "policy": dict(PROMOTION_POLICY)}
