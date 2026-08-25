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

FEATURE_SCHEMA_VERSION = "stonks.ranker_features.v1"
MODEL_KIND = "listwise_linear_softmax_v1"
HORIZONS = {"1h", "1d", "5d"}
DEFAULT_HORIZON = os.getenv("RANKER_HORIZON", "1h")
FEATURE_NAMES = (
    "baseline_score",
    "log_price",
    "change_pct",
    "momentum_5m_pct",
    "momentum_15m_pct",
    "log_relative_volume",
    "relative_volume_missing",
    "log_recent_relative_volume",
    "recent_relative_volume_missing",
    "breakout_pct",
    "range_position",
    "log_dollar_volume",
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
            _log(relative_volume),
            float(relative_volume is None),
            _log(recent_relative_volume),
            float(recent_relative_volume is None),
            _float(row.get("breakout_pct")),
            _float(row.get("range_position"), 0.5),
            _log(row.get("dollar_volume")),
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
    shifted = values - np.max(values)
    weights = np.exp(np.clip(shifted, -60.0, 60.0))
    return weights / np.sum(weights)


def _load_groups(horizon: str) -> list[list[dict[str, Any]]]:
    if horizon not in HORIZONS:
        raise ValueError(f"Unknown ranker horizon: {horizon}")
    outcome_column = f"return_{horizon}_pct"
    with connection() as db:
        rows = db.execute(
            f"""
            SELECT s.*,r.captured_at AS run_captured_at,r.mode AS scan_mode,
                   r.candidate_rows AS expected_candidates,
                   o.{outcome_column} AS outcome
            FROM scan_snapshots s
            JOIN scan_runs r ON r.id=s.scan_run_id
            JOIN scan_outcomes o ON o.snapshot_id=s.id
            WHERE o.{outcome_column} IS NOT NULL
                  AND s.range_position IS NOT NULL
                  AND s.stale_minutes IS NOT NULL
            ORDER BY r.captured_at,s.baseline_rank,s.ticker
            """  # noqa: S608
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
) -> dict[str, float]:
    selected: list[float] = []
    baseline_selected: list[float] = []
    top1_hits = 0
    baseline_hits = 0
    losses: list[float] = []
    for group in groups:
        matrix = (_matrix(group) - means) / scales
        outcomes = np.asarray([_float(row["outcome"]) for row in group], dtype=np.float64)
        scores = matrix @ weights
        predicted = int(np.argmax(scores))
        baseline = int(np.argmax([_float(row["score"]) for row in group]))
        actual = int(np.argmax(outcomes))
        selected.append(float(outcomes[predicted]))
        baseline_selected.append(float(outcomes[baseline]))
        top1_hits += int(predicted == actual)
        baseline_hits += int(baseline == actual)
        target = _softmax(np.clip(outcomes, -30.0, 30.0) / 5.0)
        probability = _softmax(scores)
        losses.append(float(-np.sum(target * np.log(np.maximum(probability, 1e-12)))))
    count = max(1, len(groups))
    return {
        "groups": float(len(groups)),
        "mean_selected_return_pct": round(float(np.mean(selected)), 6),
        "baseline_mean_selected_return_pct": round(float(np.mean(baseline_selected)), 6),
        "top1_exact_rate": round(top1_hits / count, 6),
        "baseline_top1_exact_rate": round(baseline_hits / count, 6),
        "listwise_loss": round(float(np.mean(losses)), 6),
    }


def train_shadow_ranker(
    horizon: str = DEFAULT_HORIZON,
    *,
    min_groups: int = 20,
    min_rows: int = 200,
    epochs: int = 500,
) -> dict[str, Any]:
    """Train a small transparent ranker and save it in shadow status."""

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

    train_groups, validation_groups = _split_groups(groups)
    means, scales = _normalizer(train_groups)
    weights = np.zeros(len(FEATURE_NAMES), dtype=np.float64)
    learning_rate = 0.08
    l2 = 0.002
    for epoch in range(epochs):
        gradient = np.zeros_like(weights)
        for group in train_groups:
            matrix = (_matrix(group) - means) / scales
            outcomes = np.asarray([_float(row["outcome"]) for row in group], dtype=np.float64)
            target = _softmax(np.clip(outcomes, -30.0, 30.0) / 5.0)
            predicted = _softmax(matrix @ weights)
            gradient += matrix.T @ (predicted - target)
        gradient /= len(train_groups)
        gradient += l2 * weights
        step = learning_rate / (1.0 + epoch / 250.0)
        weights -= step * np.clip(gradient, -10.0, 10.0)

    metrics = {
        "train": _evaluate(train_groups, means, scales, weights),
        "validation": _evaluate(validation_groups, means, scales, weights),
        "feature_names": list(FEATURE_NAMES),
        "target": "softmax_of_clipped_future_return",
        "target_temperature_pct": 5.0,
        "split": "oldest_80_percent_train_newest_20_percent_validation",
    }
    artifact = {
        "feature_names": list(FEATURE_NAMES),
        "means": means.tolist(),
        "scales": scales.tolist(),
        "weights": weights.tolist(),
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
            WHERE horizon=? AND status IN ('shadow','active')
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,created_at DESC
            LIMIT 1
            """,
            (horizon,),
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
    scores = ((matrix - model.means) / model.scales) @ model.weights
    ranked = sorted(
        range(len(rows)),
        key=lambda index: (-float(scores[index]), rows[index]["ticker"]),
    )
    rank_by_index = {index: rank for rank, index in enumerate(ranked, start=1)}
    created_at = _iso()
    with connection() as db:
        db.executemany(
            """
            INSERT OR REPLACE INTO ranker_predictions(
                snapshot_id,model_id,score,rank,created_at
            ) VALUES(?,?,?,?,?)
            """,
            [
                (
                    rows[index]["id"],
                    model.id,
                    float(scores[index]),
                    rank_by_index[index],
                    created_at,
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
              (SELECT COUNT(*) FROM scan_outcomes WHERE return_5d_pct IS NOT NULL) AS labeled_5d
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
            outcomes = [_float(row["outcome"]) for row in group]
            best = max(outcomes)
            used_actions: set[int] = set()
            for row, outcome in zip(group, outcomes, strict=True):
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
                        int(math.isclose(outcome, best, abs_tol=1e-12)),
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
    train.add_argument("--min-groups", type=int, default=20)
    train.add_argument("--min-rows", type=int, default=200)
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
