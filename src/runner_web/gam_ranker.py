"""An additive model for the barrier question, exportable to integer tables.

The incumbent is a multiclass logistic model over the same fixed-point features.
This is the modern tabular alternative the review asked for, kept in the shape the
deployment can already run: a *generalised additive model*, where each feature
contributes a per-bin score to each class and the class score is the intercept
plus those contributions. Because it is additive, the whole model is a set of
integer lookup tables, so it exports to the integer runtime mechanically and each
feature's curve can be read straight off the artifact -- which is what makes a
"why this name" panel honest rather than decorative.

Binning is quantile-based and fitted on training rows only, so a bin edge never
depends on data the model is scored against. Probabilities come from a softmax
over integer scores; the artifact itself is integer, so a Rust port is a lookup
and a sum.
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from typing import Any

import numpy as np

from runner_web.database import DatabaseConnection
from runner_web.db import connection
from runner_web.labels import barrier_contract
from runner_web.ranker import (
    CLASS_NAMES,
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    _load_groups,
    _run_rust,
    load_latest_model,
    register_served_model,
)

MODEL_KIND = "integer_additive_barrier_v1"
ARTIFACT_SCHEMA = "stonks.integer_gam.v1"
BINS = int(os.getenv("GAM_RANKER_BINS", "32"))
EPOCHS = int(os.getenv("GAM_RANKER_EPOCHS", "300"))
LEARNING_RATE = float(os.getenv("GAM_RANKER_LEARNING_RATE", "0.05"))
L2 = float(os.getenv("GAM_RANKER_L2", "0.01"))
SEED = 20260921
# Contributions are stored as millionths, so the artifact is integer and the
# Python and Rust scorers can agree exactly.
SCALE = 1_000_000
# A challenger has to beat the incumbent by this much log loss to be promoted.
PROMOTION_MARGIN = float(os.getenv("GAM_RANKER_PROMOTION_MARGIN", "0.005"))


def quantile_edges(column: np.ndarray, *, bins: int = BINS) -> list[int]:
    """Bin edges from training values only, as integers the artifact can hold."""

    values = np.asarray(column, dtype=np.float64)
    if values.size == 0:
        return []
    edges = np.quantile(values, np.linspace(0, 1, bins + 1)[1:-1])
    unique: list[int] = []
    for edge in np.round(edges).astype(np.int64).tolist():
        if not unique or edge > unique[-1]:
            unique.append(int(edge))
    return unique


def bin_index(value: int, edges: list[int]) -> int:
    """Which bin a fixed-point feature falls in, by binary search over edges."""

    low, high = 0, len(edges)
    while low < high:
        middle = (low + high) // 2
        if value <= edges[middle]:
            high = middle
        else:
            low = middle + 1
    return low


def build_tables(
    features: np.ndarray, targets: np.ndarray, *, bins: int = BINS
) -> dict[str, Any]:
    """Fit the additive tables: one contribution row per feature per bin."""

    rows, columns = features.shape
    edges = [quantile_edges(features[:, index], bins=bins) for index in range(columns)]
    binned = np.zeros((rows, columns), dtype=np.int64)
    for index in range(columns):
        for row in range(rows):
            binned[row, index] = bin_index(int(features[row, index]), edges[index])
    sizes = [len(edge_list) + 1 for edge_list in edges]
    classes = len(CLASS_NAMES)
    tables = [np.zeros((sizes[index], classes)) for index in range(columns)]
    intercept = np.zeros(classes)
    # Deterministic Adam, full batch: the problem is small and the tables are
    # the whole model, so this trains in well under a second.
    moment1 = [np.zeros_like(table) for table in tables]
    moment2 = [np.zeros_like(table) for table in tables]
    intercept1 = np.zeros(classes)
    intercept2 = np.zeros(classes)
    beta1, beta2, epsilon = 0.9, 0.999, 1e-8
    for epoch in range(1, max(1, EPOCHS) + 1):
        scores = np.tile(intercept, (rows, 1))
        for index in range(columns):
            scores += tables[index][binned[:, index]]
        scores -= scores.max(axis=1, keepdims=True)
        probabilities = np.exp(scores)
        probabilities /= probabilities.sum(axis=1, keepdims=True)
        grad_out = probabilities - np.eye(classes)[targets]
        grad_intercept = grad_out.mean(axis=0)
        grads = []
        for index in range(columns):
            table_grad = np.zeros_like(tables[index])
            np.add.at(table_grad, binned[:, index], grad_out)
            table_grad = table_grad / rows + L2 * tables[index]
            grads.append(table_grad)
        for index in range(columns):
            moment1[index] = beta1 * moment1[index] + (1 - beta1) * grads[index]
            moment2[index] = beta2 * moment2[index] + (1 - beta2) * grads[index] ** 2
            step = moment1[index] / (1 - beta1**epoch)
            scale = np.sqrt(moment2[index] / (1 - beta2**epoch)) + epsilon
            tables[index] -= LEARNING_RATE * step / scale
        intercept1 = beta1 * intercept1 + (1 - beta1) * grad_intercept
        intercept2 = beta2 * intercept2 + (1 - beta2) * grad_intercept**2
        intercept -= (
            LEARNING_RATE
            * (intercept1 / (1 - beta1**epoch))
            / (np.sqrt(intercept2 / (1 - beta2**epoch)) + epsilon)
        )
    return {
        "edges": edges,
        "tables": [table.tolist() for table in tables],
        "intercept": intercept.tolist(),
    }


def integer_artifact(model: dict[str, Any]) -> dict[str, Any]:
    """The trained tables as integers, ready for the integer runtime."""

    return {
        "schema": ARTIFACT_SCHEMA,
        "model_kind": MODEL_KIND,
        # The artifact names exactly the features it carries tables for.
        "feature_names": list(FEATURE_NAMES)[: len(model["tables"])],
        "classes": list(CLASS_NAMES),
        "scale": SCALE,
        "edges": [[int(edge) for edge in edge_list] for edge_list in model["edges"]],
        "tables": [
            [[int(round(value * SCALE)) for value in row] for row in table]
            for table in model["tables"]
        ],
        "intercept": [int(round(value * SCALE)) for value in model["intercept"]],
        "label_contract": barrier_contract(),
    }


def score_integers(artifact: dict[str, Any], features: list[int]) -> list[float]:
    """Class scores as integers would be summed, returned in model units."""

    totals = [float(value) for value in artifact["intercept"]]
    for index, value in enumerate(features):
        if index >= len(artifact["tables"]):
            break
        edges = artifact["edges"][index]
        table = artifact["tables"][index]
        position = bin_index(int(value), edges)
        row = table[min(position, len(table) - 1)]
        for class_index in range(len(totals)):
            totals[class_index] += float(row[class_index])
    scale = float(artifact.get("scale") or 1)
    return [value / scale for value in totals]


def probabilities_from_scores(scores: list[float]) -> list[float]:
    largest = max(scores)
    exps = [math.exp(value - largest) for value in scores]
    total = sum(exps) or 1.0
    return [value / total for value in exps]


def predict(artifact: dict[str, Any], features: list[int]) -> list[float]:
    """Class probabilities for one fixed-point feature vector."""

    return probabilities_from_scores(score_integers(artifact, features))


def contributions(
    artifact: dict[str, Any], features: list[int], *, top: int = 4
) -> list[dict[str, Any]]:
    """The strongest per-feature pushes on the leading class, for a why-panel."""

    scores = score_integers(artifact, features)
    leader = int(np.argmax(scores))
    rows = []
    for index, name in enumerate(artifact.get("feature_names") or FEATURE_NAMES):
        if index >= len(artifact["tables"]):
            break
        edges = artifact["edges"][index]
        table = artifact["tables"][index]
        position = bin_index(int(features[index]), edges)
        row = table[min(position, len(table) - 1)]
        rows.append(
            {
                "feature": str(name),
                "value": int(features[index]),
                "bin": int(position),
                "points": round(float(row[leader]) / float(artifact.get("scale") or 1), 4),
            }
        )
    rows.sort(key=lambda row: abs(float(row["points"])), reverse=True)
    return rows[: max(1, top)]


def _targets(groups: list[list[dict[str, Any]]]) -> tuple[np.ndarray, np.ndarray]:
    rows = [row for group in groups for row in group]
    features = np.asarray([list(row["feature_vector"]) for row in rows], dtype=np.float64)
    targets = np.asarray([CLASS_NAMES.index(str(row["outcome"])) for row in rows], dtype=np.int64)
    return features, targets


def metrics(probabilities: np.ndarray, targets: np.ndarray) -> dict[str, Any]:
    """Proper scoring losses, plus the base rate they must beat."""

    classes = len(CLASS_NAMES)
    clipped = np.clip(probabilities, 1e-12, 1.0)
    one_hot = np.eye(classes)[targets]
    log_loss = float(-np.mean(np.log(clipped[np.arange(len(targets)), targets])))
    brier = float(np.mean(np.sum((clipped - one_hot) ** 2, axis=1)))
    prior = np.bincount(targets, minlength=classes) / max(1, len(targets))
    base_loss = float(-np.mean(np.log(np.clip(prior[targets], 1e-12, 1.0))))
    accuracy = float(np.mean(np.argmax(clipped, axis=1) == targets))
    confidence = clipped.max(axis=1)
    correct = (np.argmax(clipped, axis=1) == targets).astype(np.float64)
    ece = float(np.mean(np.abs(confidence - correct))) if len(targets) else 0.0
    return {
        "rows": int(len(targets)),
        "log_loss": round(log_loss, 6),
        "brier": round(brier, 6),
        "base_log_loss": round(base_loss, 6),
        "accuracy": round(accuracy, 4),
        "base_accuracy": round(float(np.max(prior)), 4),
        "calibration_gap": round(ece, 6),
    }


def train_and_store(
    database: DatabaseConnection | None = None,
    *,
    horizon: str = "60m",
    maximum_groups: int = 200,
) -> dict[str, Any]:
    """Train on the same purged groups as the incumbent, then compare honestly."""

    groups = _load_groups(horizon, maximum_groups=maximum_groups)
    if len(groups) < 6:
        return {"status": "insufficient", "groups": len(groups)}
    split = max(1, int(len(groups) * 0.8))
    train_groups, test_groups = groups[:split], groups[split:]
    if not test_groups:
        return {"status": "insufficient", "groups": len(groups)}
    features, targets = _targets(train_groups)
    test_features, test_targets = _targets(test_groups)
    model = build_tables(features, targets)
    artifact = integer_artifact(model)
    scored = np.asarray([predict(artifact, list(row)) for row in test_features])
    challenger = metrics(scored, test_targets)
    incumbent = _incumbent_metrics(test_groups)
    promoted = bool(
        incumbent.get("available")
        and challenger["log_loss"] < incumbent["log_loss"] - PROMOTION_MARGIN
        and challenger["log_loss"] < challenger["base_log_loss"]
    )
    moment = datetime.now(UTC).isoformat()
    model_id = f"gam-{moment.replace(':', '').replace('-', '')[:15]}"
    _store(database, model_id, artifact, challenger, incumbent, promoted, moment)
    return {
        "status": "trained",
        "model_id": model_id,
        "groups": len(groups),
        "rows": int(len(features)),
        "promoted": promoted,
        "challenger": challenger,
        "incumbent": incumbent,
    }


def _incumbent_metrics(test_groups: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """The active model's losses on the same held-out rows, via the runtime."""

    model = load_latest_model()
    if model is None:
        return {"available": False}
    rows = [row for group in test_groups for row in group]
    try:
        response = _run_rust(
            {
                "command": "predict",
                "artifact": model.artifact,
                "rows": [
                    {
                        "id": str(row["snapshot_id"]),
                        "ticker": str(row["ticker"]),
                        "features": list(row["feature_vector"]),
                    }
                    for row in rows
                ],
            }
        )
    except Exception as exc:  # pragma: no cover - runtime unavailable in some environments
        return {"available": False, "reason": str(exc)[:120]}
    by_id = {str(item.get("id")): item for item in response.get("predictions") or []}
    probabilities = []
    targets = []
    for row in rows:
        item = by_id.get(str(row["snapshot_id"]))
        if not item:
            continue
        probabilities.append(
            [
                float(item.get("probability_down") or 0.0),
                float(item.get("probability_timeout") or 0.0),
                float(item.get("probability_up") or 0.0),
            ]
        )
        targets.append(CLASS_NAMES.index(str(row["outcome"])))
    if not probabilities:
        return {"available": False, "reason": "no_predictions"}
    report = metrics(np.asarray(probabilities), np.asarray(targets))
    report["available"] = True
    report["model_id"] = model.id
    return report


def _store(
    database: DatabaseConnection | None,
    model_id: str,
    artifact: dict[str, Any],
    challenger: dict[str, Any],
    incumbent: dict[str, Any],
    promoted: bool,
    moment: str,
) -> None:
    payload = (
        model_id,
        FEATURE_SCHEMA_VERSION,
        MODEL_KIND,
        json.dumps(artifact, separators=(",", ":")),
        json.dumps({"challenger": challenger, "incumbent": incumbent}, separators=(",", ":")),
        int(challenger.get("rows") or 0),
        "active" if promoted else "shadow",
        moment,
    )
    statement = """
        INSERT INTO gam_ranker_models(
            id,feature_schema_version,model_kind,artifact_json,metrics_json,
            training_rows,status,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO NOTHING
        """
    if database is not None:
        database.execute(statement, payload)
        if promoted:
            database.execute(
                "UPDATE gam_ranker_models SET status='retired' WHERE status='active' AND id<>?",
                (model_id,),
            )
            _serve(model_id, artifact, challenger, incumbent, moment, database)
        return
    with connection() as db:
        db.execute(statement, payload)
        if promoted:
            db.execute(
                "UPDATE gam_ranker_models SET status='retired' WHERE status='active' AND id<>?",
                (model_id,),
            )
            _serve(model_id, artifact, challenger, incumbent, moment, db)


def _serve(
    model_id: str,
    artifact: dict[str, Any],
    challenger: dict[str, Any],
    incumbent: dict[str, Any],
    moment: str,
    database: DatabaseConnection,
) -> None:
    """A promoted additive model becomes the served one."""

    register_served_model(
        model_id,
        MODEL_KIND,
        artifact,
        {"challenger": challenger, "incumbent": incumbent, "model_kind": MODEL_KIND},
        training_groups=int(challenger.get("rows") or 0),
        training_rows=int(challenger.get("rows") or 0),
        created_at=moment,
        connection=database,
    )


_ACTIVE_CACHE: tuple[float, dict[str, Any] | None] | None = None


def active_model(*, refresh: bool = False) -> dict[str, Any] | None:
    """The promoted additive model, if one has earned it."""

    global _ACTIVE_CACHE
    moment = datetime.now(UTC).timestamp()
    if not refresh and _ACTIVE_CACHE and moment - _ACTIVE_CACHE[0] < 300:
        return _ACTIVE_CACHE[1]
    model: dict[str, Any] | None = None
    try:
        with connection() as database:
            row = database.execute(
                """
                SELECT id,artifact_json,metrics_json,created_at FROM gam_ranker_models
                WHERE status='active' AND model_kind=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (MODEL_KIND,),
            ).fetchone()
        if row:
            model = {
                "id": str(row["id"]),
                "artifact": json.loads(str(row["artifact_json"])),
                "metrics": json.loads(str(row["metrics_json"])),
                "created_at": str(row["created_at"]),
            }
    except Exception:
        model = None
    _ACTIVE_CACHE = (moment, model)
    return model


def main(argv: list[str] | None = None) -> int:
    """Console entry point: train the additive challenger and report the compare."""

    import argparse

    parser = argparse.ArgumentParser(description="Train the additive barrier model")
    parser.add_argument("command", nargs="?", default="train", choices=("train", "metrics"))
    args = parser.parse_args(argv)
    if args.command == "metrics":
        model = active_model(refresh=True)
        print(json.dumps(model["metrics"] if model else {"status": "no active model"}))
        return 0
    result = train_and_store()
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result.get("status") == "trained" else 1


if __name__ == "__main__":
    raise SystemExit(main())