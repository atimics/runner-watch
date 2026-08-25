from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from runner_web.db import connection, init_db

FEATURE_SCHEMA_VERSION = "stonks.ranker_features.v2"
MODEL_KIND = "multiclass_logistic_barrier_v2"
HORIZONS = {"60m"}
_configured_horizon = os.getenv("RANKER_HORIZON", "60m")
DEFAULT_HORIZON = "60m" if _configured_horizon == "1h" else _configured_horizon
CLASS_NAMES = ("down", "timeout", "up")
CLASS_INDEX = {name: index for index, name in enumerate(CLASS_NAMES)}
FEATURE_NAMES = (
    "baseline_score",
    "log_price",
    "change_pct",
    "momentum_5m_pct",
    "momentum_15m_pct",
    "momentum_previous_5m_pct",
    "momentum_acceleration_pct",
    "intraday_volatility_pct",
    "log_relative_volume",
    "relative_volume_missing",
    "log_recent_relative_volume",
    "recent_relative_volume_missing",
    "breakout_pct",
    "range_position",
    "vwap_position_pct",
    "pullback_from_high_pct",
    "close_location",
    "log_dollar_volume",
    "log_recent_dollar_volume",
    "log_average_dollar_volume",
    "stale_minutes",
    "session_pre_market",
    "session_regular",
    "session_after_hours",
    "mode_low_price",
    "catalyst_score",
    "catalyst_missing",
    "catalyst_positive",
    "catalyst_risk",
)


@dataclass(frozen=True, slots=True)
class RankerModel:
    id: str
    horizon: str
    means: np.ndarray
    scales: np.ndarray
    weights: np.ndarray
    bias: np.ndarray
    temperature: float
    timeout_return_pct: float
    metrics: dict[str, Any]


def _iso() -> str:
    return datetime.now(UTC).isoformat()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _log(value: Any) -> float:
    return math.log1p(max(0.0, _float(value)))


def feature_vector(row: dict[str, Any]) -> np.ndarray:
    relative_volume = row.get("relative_volume")
    recent_relative_volume = row.get("recent_relative_volume")
    catalyst_score = row.get("catalyst_score")
    session = str(row.get("session") or "").upper()
    sentiment = str(row.get("catalyst_sentiment") or "").lower()
    scan_mode = str(row.get("scan_mode") or "penny").lower()
    return np.asarray(
        [
            _float(row.get("score")),
            _log(row.get("price")),
            _float(row.get("change_pct")),
            _float(row.get("momentum_5m_pct")),
            _float(row.get("momentum_15m_pct")),
            _float(row.get("momentum_previous_5m_pct")),
            _float(row.get("momentum_acceleration_pct")),
            _float(row.get("intraday_volatility_pct")),
            _log(relative_volume),
            float(relative_volume is None),
            _log(recent_relative_volume),
            float(recent_relative_volume is None),
            _float(row.get("breakout_pct")),
            _float(row.get("range_position"), 0.5),
            _float(row.get("vwap_position_pct")),
            _float(row.get("pullback_from_high_pct")),
            _float(row.get("close_location"), 0.5),
            _log(row.get("dollar_volume")),
            _log(row.get("recent_dollar_volume")),
            _log(row.get("average_dollar_volume")),
            min(240.0, max(0.0, _float(row.get("stale_minutes")))),
            float(session == "PRE-MARKET"),
            float(session == "REGULAR"),
            float(session == "AFTER-HOURS"),
            float(scan_mode == "low_price"),
            _float(catalyst_score),
            float(catalyst_score is None),
            float(sentiment == "positive"),
            float(sentiment == "risk"),
        ],
        dtype=np.float64,
    )


def _softmax(values: np.ndarray) -> np.ndarray:
    shifted = values - np.max(values, axis=-1, keepdims=True)
    weights = np.exp(np.clip(shifted, -60.0, 60.0))
    return weights / np.sum(weights, axis=-1, keepdims=True)


def _load_groups(horizon: str) -> list[list[dict[str, Any]]]:
    if horizon not in HORIZONS:
        raise ValueError(f"Unknown ranker horizon: {horizon}")
    with connection() as db:
        rows = db.execute(
            """
            SELECT s.*,r.captured_at AS run_captured_at,r.mode AS scan_mode,
                   r.candidate_rows AS expected_candidates,
                   o.barrier_label AS outcome,o.return_60m_pct AS outcome_return
            FROM scan_snapshots s
            JOIN scan_runs r ON r.id=s.scan_run_id
            JOIN scan_outcomes o ON o.snapshot_id=s.id
            WHERE o.barrier_label IN ('down','timeout','up')
                  AND s.range_position IS NOT NULL
                  AND s.stale_minutes IS NOT NULL
            ORDER BY r.captured_at,s.baseline_rank,s.ticker
            """
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for raw in rows:
        row = dict(raw)
        group_id = str(row["scan_run_id"])
        if group_id not in grouped:
            order.append(group_id)
        grouped[group_id].append(row)
    complete: list[list[dict[str, Any]]] = []
    for group_id in order:
        group = grouped[group_id]
        expected = int(group[0]["expected_candidates"])
        if len(group) == expected and len(group) >= 2:
            complete.append(group)
    return complete


def _split_groups(
    groups: list[list[dict[str, Any]]], validation_fraction: float = 0.2
) -> tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
    validation_count = max(1, int(round(len(groups) * validation_fraction)))
    validation_count = min(validation_count, max(1, len(groups) - 2))
    return groups[:-validation_count], groups[-validation_count:]


def _matrix(group: list[dict[str, Any]]) -> np.ndarray:
    return np.vstack([feature_vector(row) for row in group])


def _normalizer(groups: list[list[dict[str, Any]]]) -> tuple[np.ndarray, np.ndarray]:
    rows = np.vstack([_matrix(group) for group in groups])
    means = np.mean(rows, axis=0)
    scales = np.std(rows, axis=0)
    scales[scales < 1e-8] = 1.0
    return means, scales


def _evaluate(
    groups: list[list[dict[str, Any]]],
    means: np.ndarray,
    scales: np.ndarray,
    weights: np.ndarray,
    bias: np.ndarray,
    temperature: float,
    timeout_return_pct: float,
) -> dict[str, float]:
    selected_returns: list[float] = []
    baseline_returns: list[float] = []
    selected_wins = 0
    baseline_wins = 0
    selected_rows = 0
    top5_wins = 0
    baseline_top5_wins = 0
    top5_rows = 0
    all_probabilities: list[np.ndarray] = []
    all_targets: list[int] = []
    for group in groups:
        matrix = (_matrix(group) - means) / scales
        logits = matrix @ weights + bias
        probabilities = _softmax(logits / temperature)
        expected = (
            -4.0 * probabilities[:, CLASS_INDEX["down"]]
            + timeout_return_pct * probabilities[:, CLASS_INDEX["timeout"]]
            + 8.0 * probabilities[:, CLASS_INDEX["up"]]
        )
        predicted = int(np.argmax(expected))
        baseline = int(np.argmax([_float(row["score"]) for row in group]))
        selected_wins += int(group[predicted]["outcome"] == "up")
        baseline_wins += int(group[baseline]["outcome"] == "up")
        selected_rows += 1
        selected_return = group[predicted].get("outcome_return")
        baseline_return = group[baseline].get("outcome_return")
        if selected_return is not None:
            selected_returns.append(_float(selected_return))
        if baseline_return is not None:
            baseline_returns.append(_float(baseline_return))

        top_count = min(5, len(group))
        model_top = np.argsort(-expected)[:top_count]
        baseline_top = sorted(
            range(len(group)), key=lambda index: -_float(group[index]["score"])
        )[:top_count]
        top5_wins += sum(group[index]["outcome"] == "up" for index in model_top)
        baseline_top5_wins += sum(
            group[index]["outcome"] == "up" for index in baseline_top
        )
        top5_rows += top_count
        all_probabilities.extend(probabilities)
        all_targets.extend(CLASS_INDEX[str(row["outcome"])] for row in group)

    probability_matrix = np.vstack(all_probabilities)
    target_array = np.asarray(all_targets, dtype=np.int64)
    chosen = probability_matrix[np.arange(len(target_array)), target_array]
    log_loss = float(-np.mean(np.log(np.maximum(chosen, 1e-12))))
    up_target = (target_array == CLASS_INDEX["up"]).astype(np.float64)
    up_probability = probability_matrix[:, CLASS_INDEX["up"]]
    brier = float(np.mean((up_probability - up_target) ** 2))
    calibration_error = 0.0
    for start in np.linspace(0.0, 0.9, 10):
        mask = (up_probability >= start) & (up_probability < start + 0.1)
        if np.any(mask):
            calibration_error += float(np.mean(mask)) * abs(
                float(np.mean(up_probability[mask])) - float(np.mean(up_target[mask]))
            )
    return {
        "groups": float(len(groups)),
        "rows": float(len(target_array)),
        "selected_up_rate": round(selected_wins / max(1, selected_rows), 6),
        "baseline_selected_up_rate": round(baseline_wins / max(1, selected_rows), 6),
        "precision_at_5": round(top5_wins / max(1, top5_rows), 6),
        "baseline_precision_at_5": round(
            baseline_top5_wins / max(1, top5_rows), 6
        ),
        "mean_selected_return_pct": round(float(np.mean(selected_returns)), 6)
        if selected_returns
        else 0.0,
        "baseline_mean_selected_return_pct": round(
            float(np.mean(baseline_returns)), 6
        )
        if baseline_returns
        else 0.0,
        "multiclass_log_loss": round(log_loss, 6),
        "up_brier_score": round(brier, 6),
        "up_expected_calibration_error": round(calibration_error, 6),
    }


def _calibrate_temperature(
    groups: list[list[dict[str, Any]]],
    means: np.ndarray,
    scales: np.ndarray,
    weights: np.ndarray,
    bias: np.ndarray,
) -> float:
    logits = np.vstack([((_matrix(group) - means) / scales) @ weights + bias for group in groups])
    targets = np.asarray(
        [CLASS_INDEX[str(row["outcome"])] for group in groups for row in group],
        dtype=np.int64,
    )
    best_temperature = 1.0
    best_loss = math.inf
    for temperature in np.linspace(0.5, 3.0, 51):
        probabilities = _softmax(logits / temperature)
        chosen = probabilities[np.arange(len(targets)), targets]
        loss = float(-np.mean(np.log(np.maximum(chosen, 1e-12))))
        if loss < best_loss:
            best_loss = loss
            best_temperature = float(temperature)
    return best_temperature


def train_shadow_ranker(
    horizon: str = DEFAULT_HORIZON,
    *,
    min_groups: int = 40,
    min_rows: int = 1_000,
    epochs: int = 500,
) -> dict[str, Any]:
    """Train a calibrated barrier-probability model in shadow status."""

    groups = _load_groups(horizon)
    row_count = sum(len(group) for group in groups)
    if len(groups) < min_groups or row_count < min_rows:
        return {
            "trained": False,
            "reason": "not_enough_labeled_data",
            "groups": len(groups),
            "rows": row_count,
            "minimum_groups": min_groups,
            "minimum_rows": min_rows,
        }
    outcome_counts = {
        label: sum(row["outcome"] == label for group in groups for row in group)
        for label in CLASS_NAMES
    }
    minimum_per_class = min(20, max(2, min_rows // 50))
    if min(outcome_counts.values()) < minimum_per_class:
        return {
            "trained": False,
            "reason": "not_enough_examples_per_outcome",
            "groups": len(groups),
            "rows": row_count,
            "outcomes": outcome_counts,
            "minimum_per_outcome": minimum_per_class,
        }

    train_groups, validation_groups = _split_groups(groups)
    means, scales = _normalizer(train_groups)
    weights = np.zeros((len(FEATURE_NAMES), len(CLASS_NAMES)), dtype=np.float64)
    class_counts = np.ones(len(CLASS_NAMES), dtype=np.float64)
    for group in train_groups:
        for row in group:
            class_counts[CLASS_INDEX[str(row["outcome"])]] += 1
    bias = np.log(class_counts / np.sum(class_counts))
    learning_rate = 0.06
    l2 = 0.003
    for epoch in range(epochs):
        gradient = np.zeros_like(weights)
        bias_gradient = np.zeros_like(bias)
        for group in train_groups:
            matrix = (_matrix(group) - means) / scales
            target = np.zeros((len(group), len(CLASS_NAMES)), dtype=np.float64)
            target[
                np.arange(len(group)),
                [CLASS_INDEX[str(row["outcome"])] for row in group],
            ] = 1.0
            predicted = _softmax(matrix @ weights + bias)
            difference = (predicted - target) / len(group)
            gradient += matrix.T @ difference
            bias_gradient += np.sum(difference, axis=0)
        gradient /= len(train_groups)
        bias_gradient /= len(train_groups)
        gradient += l2 * weights
        step = learning_rate / (1.0 + epoch / 250.0)
        weights -= step * np.clip(gradient, -10.0, 10.0)
        bias -= step * np.clip(bias_gradient, -10.0, 10.0)

    temperature = _calibrate_temperature(
        validation_groups, means, scales, weights, bias
    )
    timeout_returns = [
        _float(row["outcome_return"])
        for group in train_groups
        for row in group
        if row["outcome"] == "timeout" and row.get("outcome_return") is not None
    ]
    timeout_return_pct = (
        float(np.clip(np.median(timeout_returns), -4.0, 8.0))
        if timeout_returns
        else 0.0
    )

    metrics = {
        "train": _evaluate(
            train_groups,
            means,
            scales,
            weights,
            bias,
            temperature,
            timeout_return_pct,
        ),
        "validation": _evaluate(
            validation_groups,
            means,
            scales,
            weights,
            bias,
            temperature,
            timeout_return_pct,
        ),
        "feature_names": list(FEATURE_NAMES),
        "target": "hit_plus_8_before_minus_4_within_60_minutes",
        "class_names": list(CLASS_NAMES),
        "temperature": temperature,
        "timeout_return_pct": timeout_return_pct,
        "split": "oldest_80_percent_train_newest_20_percent_validation",
    }
    artifact = {
        "feature_names": list(FEATURE_NAMES),
        "means": means.tolist(),
        "scales": scales.tolist(),
        "weights": weights.tolist(),
        "bias": bias.tolist(),
        "temperature": temperature,
        "timeout_return_pct": timeout_return_pct,
    }
    created_at = _iso()
    identity = hashlib.sha256(
        json.dumps(
            {
                "artifact": artifact,
                "horizon": horizon,
                "training_end": groups[-1][0]["run_captured_at"],
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:20]
    model_id = f"ranker-{identity}"
    with connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO ranker_models(
                id,feature_schema_version,horizon,model_kind,weights_json,metrics_json,
                training_start,training_end,training_groups,training_rows,status,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                model_id,
                FEATURE_SCHEMA_VERSION,
                horizon,
                MODEL_KIND,
                json.dumps(artifact, separators=(",", ":")),
                json.dumps(metrics, separators=(",", ":")),
                str(groups[0][0]["run_captured_at"]),
                str(groups[-1][0]["run_captured_at"]),
                len(train_groups),
                sum(len(group) for group in train_groups),
                "shadow",
                created_at,
            ),
        )
    return {"trained": True, "model_id": model_id, "metrics": metrics}


def load_latest_model(horizon: str = DEFAULT_HORIZON) -> RankerModel | None:
    with connection() as db:
        row = db.execute(
            """
            SELECT * FROM ranker_models
            WHERE horizon=? AND feature_schema_version=? AND model_kind=?
                  AND status IN ('shadow','active')
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,created_at DESC
            LIMIT 1
            """,
            (horizon, FEATURE_SCHEMA_VERSION, MODEL_KIND),
        ).fetchone()
    if not row:
        return None
    artifact = json.loads(str(row["weights_json"]))
    if tuple(artifact.get("feature_names", [])) != FEATURE_NAMES:
        return None
    return RankerModel(
        id=str(row["id"]),
        horizon=str(row["horizon"]),
        means=np.asarray(artifact["means"], dtype=np.float64),
        scales=np.asarray(artifact["scales"], dtype=np.float64),
        weights=np.asarray(artifact["weights"], dtype=np.float64),
        bias=np.asarray(artifact["bias"], dtype=np.float64),
        temperature=float(artifact.get("temperature", 1.0)),
        timeout_return_pct=float(artifact.get("timeout_return_pct", 0.0)),
        metrics=json.loads(str(row["metrics_json"])),
    )


def predict_and_store(scan_run_id: str, model: RankerModel | None = None) -> dict[str, Any]:
    model = model or load_latest_model()
    if model is None:
        return {"predicted": False, "reason": "no_shadow_model"}
    with connection() as db:
        rows = [
            dict(row)
            for row in db.execute(
                """
                SELECT s.*,r.mode AS scan_mode FROM scan_snapshots s
                JOIN scan_runs r ON r.id=s.scan_run_id
                WHERE s.scan_run_id=? ORDER BY s.baseline_rank,s.ticker
                """,
                (scan_run_id,),
            ).fetchall()
        ]
    if not rows:
        return {"predicted": False, "reason": "empty_scan"}
    matrix = np.vstack([feature_vector(row) for row in rows])
    logits = ((matrix - model.means) / model.scales) @ model.weights + model.bias
    probabilities = _softmax(logits / model.temperature)
    expected_returns = (
        -4.0 * probabilities[:, CLASS_INDEX["down"]]
        + model.timeout_return_pct * probabilities[:, CLASS_INDEX["timeout"]]
        + 8.0 * probabilities[:, CLASS_INDEX["up"]]
    )
    ranked = sorted(
        range(len(rows)),
        key=lambda index: (-float(expected_returns[index]), rows[index]["ticker"]),
    )
    rank_by_index = {index: rank for rank, index in enumerate(ranked, start=1)}
    created_at = _iso()
    with connection() as db:
        db.executemany(
            """
            INSERT OR REPLACE INTO ranker_predictions(
                snapshot_id,model_id,score,rank,created_at,probability_up,
                probability_down,probability_timeout,expected_return_pct
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    rows[index]["id"],
                    model.id,
                    float(probabilities[index, CLASS_INDEX["up"]] * 100),
                    rank_by_index[index],
                    created_at,
                    float(probabilities[index, CLASS_INDEX["up"]]),
                    float(probabilities[index, CLASS_INDEX["down"]]),
                    float(probabilities[index, CLASS_INDEX["timeout"]]),
                    float(expected_returns[index]),
                )
                for index in range(len(rows))
            ],
        )
    return {"predicted": True, "model_id": model.id, "rows": len(rows)}


def ranker_status() -> dict[str, Any]:
    with connection() as db:
        counts = db.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM scan_runs) AS scan_runs,
              (SELECT COUNT(*) FROM scan_snapshots WHERE scan_run_id IS NOT NULL) AS candidates,
              (SELECT COUNT(*) FROM scan_outcomes WHERE return_1h_pct IS NOT NULL) AS labeled_1h,
              (SELECT COUNT(*) FROM scan_outcomes WHERE return_1d_pct IS NOT NULL) AS labeled_1d,
              (SELECT COUNT(*) FROM scan_outcomes WHERE return_5d_pct IS NOT NULL) AS labeled_5d,
              (SELECT COUNT(*) FROM scan_outcomes WHERE barrier_label IS NOT NULL)
                  AS barrier_labeled,
              (SELECT COUNT(*) FROM scan_outcomes WHERE barrier_label='up') AS barrier_up,
              (SELECT COUNT(*) FROM scan_outcomes WHERE barrier_label='down') AS barrier_down,
              (SELECT COUNT(*) FROM scan_outcomes WHERE barrier_label='timeout')
                  AS barrier_timeout
            """
        ).fetchone()
    model = load_latest_model()
    return {
        **dict(counts),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "model": (
            {"id": model.id, "horizon": model.horizon, "status": "shadow", "metrics": model.metrics}
            if model
            else None
        ),
    }


def _stable_id(value: str, bits: int) -> int:
    size = bits // 8
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:size], "big")


def export_crl_dataset(path: Path, horizon: str = DEFAULT_HORIZON) -> dict[str, Any]:
    """Write the grouped CSV contract consumed by crlplrimes' generic scorer."""

    groups = _load_groups(horizon)
    if not groups:
        raise ValueError("No complete labeled scan groups are available")
    train_end = max(1, int(len(groups) * 0.7))
    valid_end = max(train_end, int(len(groups) * 0.85))
    normalizer_groups = groups[:train_end]
    means, scales = _normalizer(normalizer_groups)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(["split", "group_id", "state_hash", "action_id", "target", *FEATURE_NAMES])
        for group_index, group in enumerate(groups, start=1):
            if group_index <= train_end:
                split = "train"
            elif group_index <= valid_end:
                split = "valid"
            else:
                split = "test"
            used_actions: set[int] = set()
            for row in group:
                action_id = _stable_id(str(row["ticker"]), 32)
                while action_id in used_actions:
                    action_id = (action_id + 1) & 0xFFFFFFFF
                used_actions.add(action_id)
                features = (feature_vector(row) - means) / scales
                writer.writerow(
                    [
                        split,
                        group_index,
                        _stable_id(str(row["scan_run_id"]), 64),
                        action_id,
                        int(row["outcome"] == "up"),
                        *[f"{value:.12g}" for value in features],
                    ]
                )
                rows_written += 1
    return {
        "path": str(path),
        "groups": len(groups),
        "rows": rows_written,
        "features": len(FEATURE_NAMES),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train and inspect the Stonks shadow ranker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    train = subparsers.add_parser("train")
    train.add_argument("--horizon", choices=sorted(HORIZONS), default=DEFAULT_HORIZON)
    train.add_argument("--min-groups", type=int, default=40)
    train.add_argument("--min-rows", type=int, default=1_000)
    export = subparsers.add_parser("export-crl")
    export.add_argument("path", type=Path)
    export.add_argument("--horizon", choices=sorted(HORIZONS), default=DEFAULT_HORIZON)
    arguments = parser.parse_args()
    init_db()
    if arguments.command == "status":
        result = ranker_status()
    elif arguments.command == "train":
        result = train_shadow_ranker(
            arguments.horizon,
            min_groups=arguments.min_groups,
            min_rows=arguments.min_rows,
        )
    else:
        result = export_crl_dataset(arguments.path, arguments.horizon)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
