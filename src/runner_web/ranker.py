from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runner_web.db import connection, init_db
from runner_web.product_policy import RANKER_TRAINING

FEATURE_SCHEMA_VERSION = "stonks.ranker_features.v4"
MODEL_KIND = "integer_multiclass_logistic_barrier_v5"
ARTIFACT_SCHEMA = "stonks.integer_ranker.v1"
HORIZONS = {"60m"}
_configured_horizon = os.getenv("RANKER_HORIZON", "60m")
DEFAULT_HORIZON = "60m" if _configured_horizon == "1h" else _configured_horizon
CLASS_NAMES = ("down", "timeout", "up")
CLASS_INDEX = {name: index for index, name in enumerate(CLASS_NAMES)}

FEATURE_SCALE = 1_000
NORMALIZED_SCALE = 1_024
PROBABILITY_SCALE = 1_000_000
RETURN_SCALE = 100
MAX_NORMALIZED_FEATURE = 16 * NORMALIZED_SCALE

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
    "opening_range_position",
    "opening_range_breakout_pct",
    "support_distance_pct",
    "support_strength",
    "resistance_distance_pct",
    "resistance_strength",
    "fib_retracement_pct",
    "fib_level_distance_pct",
    "structure_missing",
    "fibonacci_missing",
    "log_dollar_volume",
    "log_recent_dollar_volume",
    "log_average_dollar_volume",
    "stale_minutes",
    "session_pre_market",
    "session_regular",
    "session_after_hours",
    "mode_low_price",
    "mode_crash",
    "catalyst_score",
    "catalyst_missing",
    "catalyst_positive",
    "catalyst_risk",
    "drawdown_20d_pct",
    "drawdown_90d_pct",
    "drawdown_52w_pct",
    "rebound_from_20d_low_pct",
    "rug_score",
    "hard_veto",
    "crash_candidate",
    "trade_state_armed",
    "trade_state_triggered",
    "trade_state_avoid_or_exit",
    "shares_growth_pct",
    "cash_runway_months",
    "current_ratio",
    "debt_to_cash",
    "issuer_data_missing",
)


@dataclass(frozen=True, slots=True)
class RankerModel:
    id: str
    horizon: str
    artifact: dict[str, Any]
    metrics: dict[str, Any]

    @property
    def weights(self) -> tuple[tuple[int, ...], ...]:
        return tuple(tuple(int(value) for value in row) for row in self.artifact["weights"])

    @property
    def means(self) -> tuple[int, ...]:
        return tuple(int(value) for value in self.artifact["means"])

    @property
    def scales(self) -> tuple[int, ...]:
        return tuple(int(value) for value in self.artifact["scales"])


def _iso() -> str:
    return datetime.now(UTC).isoformat()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _scaled(value: Any, default: float = 0.0) -> int:
    return int(round(_float(value, default) * FEATURE_SCALE))


def _scaled_log(value: Any) -> int:
    return int(round(math.log1p(max(0.0, _float(value))) * FEATURE_SCALE))


def _flag(value: bool) -> int:
    return FEATURE_SCALE if value else 0


def feature_vector(row: dict[str, Any]) -> tuple[int, ...]:


    compact = row.get("feature_vector")
    if compact is not None:
        vector = tuple(int(value) for value in compact)
        if len(vector) != len(FEATURE_NAMES):
            raise ValueError("Stored ranker feature vector has the wrong size")
        return vector

    relative_volume = row.get("relative_volume")
    recent_relative_volume = row.get("recent_relative_volume")
    catalyst_score = row.get("catalyst_score")
    session = str(row.get("session") or "").upper()
    sentiment = str(row.get("catalyst_sentiment") or "").lower()
    scan_mode = str(row.get("scan_mode") or "penny").lower()
    trade_state = str(row.get("trade_state") or "watch").lower()
    try:
        issuer = json.loads(str(row.get("issuer_risk_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        issuer = {}
    return (
        _scaled(row.get("score")),
        _scaled_log(row.get("price")),
        _scaled(row.get("change_pct")),
        _scaled(row.get("momentum_5m_pct")),
        _scaled(row.get("momentum_15m_pct")),
        _scaled(row.get("momentum_previous_5m_pct")),
        _scaled(row.get("momentum_acceleration_pct")),
        _scaled(row.get("intraday_volatility_pct")),
        _scaled_log(relative_volume),
        _flag(relative_volume is None),
        _scaled_log(recent_relative_volume),
        _flag(recent_relative_volume is None),
        _scaled(row.get("breakout_pct")),
        _scaled(row.get("range_position"), 0.5),
        _scaled(row.get("vwap_position_pct")),
        _scaled(row.get("pullback_from_high_pct")),
        _scaled(row.get("close_location"), 0.5),
        _scaled(row.get("opening_range_position"), 0.5),
        _scaled(row.get("opening_range_breakout_pct")),
        _scaled(row.get("support_distance_pct")),
        _scaled(row.get("support_strength")),
        _scaled(row.get("resistance_distance_pct")),
        _scaled(row.get("resistance_strength")),
        _scaled(row.get("fib_retracement_pct")),
        _scaled(row.get("fib_level_distance_pct")),
        _flag(not bool(row.get("structure_available"))),
        _flag(not bool(row.get("fibonacci_available"))),
        _scaled_log(row.get("dollar_volume")),
        _scaled_log(row.get("recent_dollar_volume")),
        _scaled_log(row.get("average_dollar_volume")),
        _scaled(min(240.0, max(0.0, _float(row.get("stale_minutes"))))),
        _flag(session == "PRE-MARKET"),
        _flag(session == "REGULAR"),
        _flag(session == "AFTER-HOURS"),
        _flag(scan_mode == "low_price"),
        _flag(scan_mode == "crash"),
        _scaled(catalyst_score),
        _flag(catalyst_score is None),
        _flag(sentiment == "positive"),
        _flag(sentiment == "risk"),
        _scaled(row.get("drawdown_20d_pct")),
        _scaled(row.get("drawdown_90d_pct")),
        _scaled(row.get("drawdown_52w_pct")),
        _scaled(row.get("rebound_from_20d_low_pct")),
        _scaled(row.get("rug_score")),
        _flag(bool(row.get("hard_veto"))),
        _flag(bool(row.get("crash_candidate"))),
        _flag(trade_state == "armed"),
        _flag(trade_state in {"triggered", "manage"}),
        _flag(trade_state in {"avoid", "exit"}),
        _scaled(issuer.get("shares_growth_pct")),
        _scaled(min(36.0, max(0.0, _float(issuer.get("cash_runway_months"))))),
        _scaled(min(10.0, max(0.0, _float(issuer.get("current_ratio"))))),
        _scaled(min(20.0, max(0.0, _float(issuer.get("debt_to_cash"))))),
        _flag(not bool(issuer.get("issuer_data_available"))),
    )


def store_training_examples(
    database: Any,
    rows: list[dict[str, Any]],
    *,
    scan_mode: str,
    expected_candidates: int,
) -> int:


    if not rows:
        return 0
    database.executemany(
        """
        INSERT INTO ranker_training_examples(
            snapshot_id,scan_run_id,ticker,feature_schema_version,
            expected_candidates,captured_at,feature_vector_json,
            baseline_score_milli
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(snapshot_id) DO NOTHING
        """,
        [
            (
                str(row["id"]),
                str(row["scan_run_id"]),
                str(row["ticker"]),
                FEATURE_SCHEMA_VERSION,
                expected_candidates,
                str(row["captured_at"]),
                json.dumps(
                    feature_vector({**row, "scan_mode": scan_mode}),
                    separators=(",", ":"),
                ),
                _scaled(row.get("score")),
            )
            for row in rows
        ],
    )
    return len(rows)


def sync_training_outcome(
    database: Any,
    snapshot_id: str,
    barrier_label: str,
    outcome_return_pct: Any,
    labeled_at: str,
) -> None:


    database.execute(
        """
        UPDATE ranker_training_examples
        SET barrier_label=?,outcome_return_bp=?,labeled_at=?
        WHERE snapshot_id=?
        """,
        (
            barrier_label,
            int(round(_float(outcome_return_pct) * RETURN_SCALE)),
            labeled_at,
            snapshot_id,
        ),
    )


def _backfill_recent_training_examples(maximum_groups: int) -> int:


    with connection() as database:
        run_rows = database.execute(
            """
            SELECT r.id FROM scan_runs r
            WHERE r.feature_schema_version=?
              AND EXISTS(
                  SELECT 1 FROM scan_outcomes o
                  JOIN scan_snapshots s ON s.id=o.snapshot_id
                  WHERE s.scan_run_id=r.id
                    AND o.barrier_label IN ('down','timeout','up')
              )
            ORDER BY r.captured_at DESC,r.id DESC LIMIT ?
            """,
            (FEATURE_SCHEMA_VERSION, maximum_groups),
        ).fetchall()
        run_ids = [str(row["id"]) for row in run_rows]
        if not run_ids:
            return 0
        placeholders = ",".join("?" for _ in run_ids)
        rows = database.execute(
            f"""
            SELECT s.*,r.mode AS scan_mode,r.candidate_rows AS expected_candidates,
                   o.barrier_label,o.return_60m_pct,o.updated_at AS labeled_at
            FROM scan_snapshots s
            JOIN scan_runs r ON r.id=s.scan_run_id
            JOIN scan_outcomes o ON o.snapshot_id=s.id
            LEFT JOIN ranker_training_examples compact ON compact.snapshot_id=s.id
            WHERE s.scan_run_id IN ({placeholders})
              AND o.barrier_label IN ('down','timeout','up')
              AND s.range_position IS NOT NULL
              AND s.stale_minutes IS NOT NULL
              AND compact.snapshot_id IS NULL
            ORDER BY r.captured_at,s.baseline_rank,s.ticker
            """,
            run_ids,
        ).fetchall()
        compact_rows = []
        for raw in rows:
            row = dict(raw)
            compact_rows.append(
                (
                    str(row["id"]),
                    str(row["scan_run_id"]),
                    str(row["ticker"]),
                    FEATURE_SCHEMA_VERSION,
                    int(row["expected_candidates"]),
                    str(row["captured_at"]),
                    json.dumps(feature_vector(row), separators=(",", ":")),
                    _scaled(row.get("score")),
                    str(row["barrier_label"]),
                    int(round(_float(row.get("return_60m_pct")) * RETURN_SCALE)),
                    str(row["labeled_at"]),
                )
            )
        if compact_rows:
            database.executemany(
                """
                INSERT INTO ranker_training_examples(
                    snapshot_id,scan_run_id,ticker,feature_schema_version,
                    expected_candidates,captured_at,feature_vector_json,
                    baseline_score_milli,barrier_label,outcome_return_bp,labeled_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(snapshot_id) DO NOTHING
                """,
                compact_rows,
            )
    return len(compact_rows)


def _load_groups(
    horizon: str,
    maximum_groups: int = RANKER_TRAINING.maximum_groups,
) -> list[list[dict[str, Any]]]:
    if horizon not in HORIZONS:
        raise ValueError(f"Unknown ranker horizon: {horizon}")
    maximum_groups = max(2, maximum_groups)
    _backfill_recent_training_examples(maximum_groups)
    with connection() as database:
        complete_runs = database.execute(
            """
            SELECT scan_run_id,MIN(captured_at) AS run_captured_at
            FROM ranker_training_examples
            WHERE feature_schema_version=?
              AND barrier_label IN ('down','timeout','up')
            GROUP BY scan_run_id
            HAVING COUNT(*)=MAX(expected_candidates) AND COUNT(*)>=2
            ORDER BY run_captured_at DESC,scan_run_id DESC LIMIT ?
            """,
            (FEATURE_SCHEMA_VERSION, maximum_groups),
        ).fetchall()
        run_ids = [str(row["scan_run_id"]) for row in reversed(complete_runs)]
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        rows = database.execute(
            f"""
            SELECT * FROM ranker_training_examples
            WHERE scan_run_id IN ({placeholders})
              AND feature_schema_version=?
              AND barrier_label IN ('down','timeout','up')
            ORDER BY captured_at,scan_run_id,ticker
            """,
            (*run_ids, FEATURE_SCHEMA_VERSION),
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        row = dict(raw)
        try:
            vector = tuple(int(value) for value in json.loads(row["feature_vector_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(vector) != len(FEATURE_NAMES):
            continue
        grouped[str(row["scan_run_id"])].append(
            {
                "snapshot_id": str(row["snapshot_id"]),
                "scan_run_id": str(row["scan_run_id"]),
                "ticker": str(row["ticker"]),
                "run_captured_at": str(row["captured_at"]),
                "expected_candidates": int(row["expected_candidates"]),
                "feature_vector": vector,
                "score": int(row["baseline_score_milli"]) / FEATURE_SCALE,
                "outcome": str(row["barrier_label"]),
                "outcome_return": int(row["outcome_return_bp"] or 0) / RETURN_SCALE,
            }
        )
    complete: list[list[dict[str, Any]]] = []
    for run_id in run_ids:
        group = grouped.get(run_id, [])
        if not group:
            continue
        expected = int(group[0]["expected_candidates"])
        if len(group) == expected and len(group) >= 2:
            complete.append(group)
    return complete


def _rust_command() -> list[str]:
    configured = os.getenv("STONKS_INTEGER_RANKER_BIN", "").strip()
    if configured:
        return [configured]
    installed = shutil.which("stonks-integer-ranker")
    if installed:
        return [installed]
    root = Path(__file__).resolve().parents[2]
    crate = root / "rust" / "stonks-ranker"
    cargo = shutil.which("cargo")
    if not cargo:
        raise RuntimeError(
            "The integer ranker binary is missing. Build rust/stonks-ranker first."
        )
    return [
        cargo,
        "run",
        "--quiet",
        "--locked",
        "--target-dir",
        os.getenv(
            "STONKS_INTEGER_RANKER_TARGET_DIR",
            str(Path(tempfile.gettempdir()) / "stonks-integer-ranker-target"),
        ),
        "--manifest-path",
        str(crate / "Cargo.toml"),
        "--",
    ]


def _run_rust(request: dict[str, Any], *, timeout_seconds: int = 300) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            _rust_command(),
            input=json.dumps(request, separators=(",", ":")),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("The integer ranker timed out.") from exc
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip()[:500] or "no diagnostic output"
        raise RuntimeError(f"The integer ranker returned invalid output: {detail}") from exc
    if completed.returncode != 0 or not response.get("ok"):
        detail = str(response.get("error") or completed.stderr or "ranker failed")[:500]
        raise RuntimeError(f"The integer ranker failed: {detail}")
    return response


def _training_payload(groups: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    return [
        [
            {
                "ticker": str(row["ticker"]),
                "features": list(feature_vector(row)),
                "outcome": CLASS_INDEX[str(row["outcome"])],
                "outcome_return_bp": int(
                    round(_float(row.get("outcome_return")) * RETURN_SCALE)
                ),
                "baseline_score_milli": _scaled(row.get("score")),
            }
            for row in group
        ]
        for group in groups
    ]


def train_shadow_ranker(
    horizon: str = DEFAULT_HORIZON,
    *,
    min_groups: int = RANKER_TRAINING.minimum_groups,
    min_rows: int = RANKER_TRAINING.minimum_rows,
    maximum_groups: int = RANKER_TRAINING.maximum_groups,
    epochs: int = 500,
) -> dict[str, Any]:


    groups = _load_groups(horizon, maximum_groups)
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
    minimum_per_class = min(
        RANKER_TRAINING.minimum_per_outcome,
        max(2, min_rows // 50),
    )
    if min(outcome_counts.values()) < minimum_per_class:
        return {
            "trained": False,
            "reason": "not_enough_examples_per_outcome",
            "groups": len(groups),
            "rows": row_count,
            "outcomes": outcome_counts,
            "minimum_per_outcome": minimum_per_class,
        }

    response = _run_rust(
        {
            "command": "train",
            "feature_names": list(FEATURE_NAMES),
            "groups": _training_payload(groups),
            "epochs": max(1, epochs),
        },
        timeout_seconds=max(300, epochs * 2),
    )
    artifact = response["artifact"]
    metrics = response["metrics"]
    if not _valid_artifact(artifact):
        raise RuntimeError("The integer ranker returned an incompatible artifact.")
    identity = hashlib.sha256(
        json.dumps(
            {
                "artifact": artifact,
                "horizon": horizon,
                "training_end": groups[-1][0]["run_captured_at"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:20]
    model_id = f"ranker-{identity}"
    holdout_count = min(max(2, (len(groups) + 2) // 5), len(groups) - 1)
    training_group_count = len(groups) - holdout_count
    created_at = _iso()
    with connection() as database:
        database.execute(
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
                training_group_count,
                sum(len(group) for group in groups[:training_group_count]),
                "shadow",
                created_at,
            ),
        )
    return {
        "trained": True,
        "model_id": model_id,
        "model_kind": MODEL_KIND,
        "integer_only": True,
        "groups": len(groups),
        "rows": row_count,
        "maximum_groups": maximum_groups,
        "metrics": metrics,
    }


def train_shadow_ranker_if_due(
    horizon: str = DEFAULT_HORIZON,
    *,
    minimum_new_groups: int = RANKER_TRAINING.minimum_new_groups,
    maximum_groups: int = RANKER_TRAINING.maximum_groups,
) -> dict[str, Any]:


    _backfill_recent_training_examples(maximum_groups)
    with connection() as database:
        latest = database.execute(
            """
            SELECT training_end FROM ranker_models
            WHERE horizon=? AND feature_schema_version=? AND model_kind=?
                  AND status IN ('shadow','active')
            ORDER BY created_at DESC LIMIT 1
            """,
            (horizon, FEATURE_SCHEMA_VERSION, MODEL_KIND),
        ).fetchone()
        complete = database.execute(
            """
            SELECT MIN(captured_at) AS captured_at
            FROM ranker_training_examples
            WHERE feature_schema_version=?
              AND barrier_label IN ('down','timeout','up')
            GROUP BY scan_run_id
            HAVING COUNT(*)=MAX(expected_candidates) AND COUNT(*)>=2
            ORDER BY captured_at DESC LIMIT ?
            """,
            (FEATURE_SCHEMA_VERSION, maximum_groups),
        ).fetchall()
    latest_end = str(latest["training_end"]) if latest else None
    new_groups = sum(
        latest_end is None or str(row["captured_at"]) > latest_end for row in complete
    )
    if latest_end is not None and new_groups < max(1, minimum_new_groups):
        return {
            "trained": False,
            "reason": "waiting_for_new_groups",
            "groups": len(complete),
            "new_groups": new_groups,
            "minimum_new_groups": minimum_new_groups,
        }
    return train_shadow_ranker(horizon, maximum_groups=maximum_groups)


def _trainer_state(key: str, value: Any) -> None:
    timestamp = _iso()
    encoded = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    with connection() as database:
        database.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, encoded, timestamp),
        )


def trainer_main() -> None:


    init_db()
    interval = max(
        300,
        int(os.getenv("RANKER_TRAIN_INTERVAL_SECONDS", RANKER_TRAINING.interval_seconds)),
    )
    while True:
        try:
            result = train_shadow_ranker_if_due()
            _trainer_state("ranker_trainer_last_result", result)
            _trainer_state("ranker_trainer_last_error", "")
        except Exception as exc:
            _trainer_state("ranker_trainer_last_error", str(exc)[:1000])
        time.sleep(interval)


def _valid_artifact(artifact: Any) -> bool:
    if not isinstance(artifact, dict):
        return False
    if artifact.get("schema") != ARTIFACT_SCHEMA:
        return False
    if tuple(artifact.get("feature_names", [])) != FEATURE_NAMES:
        return False
    if artifact.get("feature_scale") != FEATURE_SCALE:
        return False
    if artifact.get("normalized_scale") != NORMALIZED_SCALE:
        return False
    if artifact.get("probability_scale") != PROBABILITY_SCALE:
        return False
    scalar_integers = all(
        isinstance(value, int) and not isinstance(value, bool)
        for key, value in artifact.items()
        if key.endswith("_scale") or key.endswith("_milli") or key.endswith("_bp")
    )
    vectors_are_integers = all(
        isinstance(value, int) and not isinstance(value, bool)
        for key in ("means", "scales", "bias")
        for value in artifact.get(key, [])
    )
    weights_are_integers = all(
        isinstance(value, int) and not isinstance(value, bool)
        for row in artifact.get("weights", [])
        for value in row
    )
    return scalar_integers and vectors_are_integers and weights_are_integers


def load_latest_model(horizon: str = DEFAULT_HORIZON) -> RankerModel | None:
    with connection() as database:
        row = database.execute(
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
    if not _valid_artifact(artifact):
        return None
    return RankerModel(
        id=str(row["id"]),
        horizon=str(row["horizon"]),
        artifact=artifact,
        metrics=json.loads(str(row["metrics_json"])),
    )


def predict_and_store(scan_run_id: str, model: RankerModel | None = None) -> dict[str, Any]:
    model = model or load_latest_model()
    if model is None:
        return {"predicted": False, "reason": "no_shadow_model"}
    with connection() as database:
        rows = [
            dict(row)
            for row in database.execute(
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
    response = _run_rust(
        {
            "command": "predict",
            "artifact": model.artifact,
            "rows": [
                {
                    "id": str(row["id"]),
                    "ticker": str(row["ticker"]),
                    "features": list(feature_vector(row)),
                }
                for row in rows
            ],
        }
    )
    predictions = response["predictions"]
    created_at = _iso()
    with connection() as database:
        database.executemany(
            """
            INSERT INTO ranker_predictions(
                snapshot_id,model_id,score,rank,created_at,probability_up,
                probability_down,probability_timeout,expected_return_pct
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(snapshot_id,model_id) DO UPDATE SET
                score=excluded.score,
                rank=excluded.rank,
                created_at=excluded.created_at,
                probability_up=excluded.probability_up,
                probability_down=excluded.probability_down,
                probability_timeout=excluded.probability_timeout,
                expected_return_pct=excluded.expected_return_pct
            """,
            [
                (
                    prediction["id"],
                    model.id,
                    int(prediction["probability_up_ppm"]) / 10_000,
                    int(prediction["rank"]),
                    created_at,
                    int(prediction["probability_up_ppm"]) / PROBABILITY_SCALE,
                    int(prediction["probability_down_ppm"]) / PROBABILITY_SCALE,
                    int(prediction["probability_timeout_ppm"]) / PROBABILITY_SCALE,
                    int(prediction["expected_return_bp"]) / RETURN_SCALE,
                )
                for prediction in predictions
            ],
        )
    return {
        "predicted": True,
        "model_id": model.id,
        "model_kind": MODEL_KIND,
        "integer_only": True,
        "rows": len(rows),
    }


def ranker_status() -> dict[str, Any]:
    with connection() as database:
        counts = database.execute(
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
        "engine": "rust_integer_fixed_point",
        "integer_only": True,
        "model_kind": MODEL_KIND,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "model": (
            {
                "id": model.id,
                "horizon": model.horizon,
                "status": "shadow",
                "metrics": model.metrics,
            }
            if model
            else None
        ),
    }


def _stable_id(value: str, bits: int) -> int:
    size = bits // 8
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:size], "big")


def _rounded_div(numerator: int, denominator: int) -> int:
    if numerator >= 0:
        return (numerator + denominator // 2) // denominator
    return -((-numerator + denominator // 2) // denominator)


def _integer_normalizer(vectors: list[tuple[int, ...]]) -> tuple[list[int], list[int]]:
    row_count = len(vectors)
    means = [
        _rounded_div(sum(vector[index] for vector in vectors), row_count)
        for index in range(len(FEATURE_NAMES))
    ]
    scales = [
        max(
            1,
            math.isqrt(
                sum((vector[index] - means[index]) ** 2 for vector in vectors) // row_count
            ),
        )
        for index in range(len(FEATURE_NAMES))
    ]
    return means, scales


def _normalize_vector(
    vector: tuple[int, ...], means: list[int], scales: list[int]
) -> tuple[int, ...]:
    return tuple(
        max(
            -MAX_NORMALIZED_FEATURE,
            min(
                MAX_NORMALIZED_FEATURE,
                _rounded_div((value - means[index]) * NORMALIZED_SCALE, scales[index]),
            ),
        )
        for index, value in enumerate(vector)
    )


def export_crl_dataset(
    path: Path,
    horizon: str = DEFAULT_HORIZON,
    maximum_groups: int = RANKER_TRAINING.maximum_groups,
) -> dict[str, Any]:


    groups = _load_groups(horizon, maximum_groups)
    if not groups:
        raise ValueError("No complete labeled scan groups are available")
    holdout_count = min(max(2, (len(groups) + 2) // 5), len(groups) - 1)
    validation_count = holdout_count // 2
    train_end = len(groups) - holdout_count
    valid_end = train_end + validation_count
    normalizer_vectors = [
        feature_vector(row) for group in groups[:train_end] for row in group
    ]
    means, scales = _integer_normalizer(normalizer_vectors)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(
            ["split", "group_id", "state_hash", "action_id", "target", *FEATURE_NAMES]
        )
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
                features = _normalize_vector(feature_vector(row), means, scales)
                writer.writerow(
                    [
                        split,
                        group_index,
                        _stable_id(str(row["scan_run_id"]), 64),
                        action_id,
                        int(row["outcome"] == "up"),
                        *features,
                    ]
                )
                rows_written += 1
    return {
        "path": str(path),
        "groups": len(groups),
        "rows": rows_written,
        "features": len(FEATURE_NAMES),
        "feature_encoding": "signed_integer_fixed_point",
        "normalized_scale": NORMALIZED_SCALE,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train and inspect the Stonks integer Rust ranker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    train = subparsers.add_parser("train")
    train.add_argument("--horizon", choices=sorted(HORIZONS), default=DEFAULT_HORIZON)
    train.add_argument(
        "--min-groups",
        type=int,
        default=RANKER_TRAINING.minimum_groups,
    )
    train.add_argument(
        "--min-rows",
        type=int,
        default=RANKER_TRAINING.minimum_rows,
    )
    train.add_argument(
        "--max-groups",
        type=int,
        default=RANKER_TRAINING.maximum_groups,
    )
    train.add_argument("--epochs", type=int, default=500)
    export = subparsers.add_parser("export-crl")
    export.add_argument("path", type=Path)
    export.add_argument("--horizon", choices=sorted(HORIZONS), default=DEFAULT_HORIZON)
    export.add_argument(
        "--max-groups",
        type=int,
        default=RANKER_TRAINING.maximum_groups,
    )
    arguments = parser.parse_args()
    init_db()
    if arguments.command == "status":
        result = ranker_status()
    elif arguments.command == "train":
        result = train_shadow_ranker(
            arguments.horizon,
            min_groups=arguments.min_groups,
            min_rows=arguments.min_rows,
            maximum_groups=arguments.max_groups,
            epochs=arguments.epochs,
        )
    else:
        result = export_crl_dataset(
            arguments.path,
            arguments.horizon,
            arguments.max_groups,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
