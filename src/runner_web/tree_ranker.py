"""Boosted trees, exported as integer trees the runtime can walk.

The benchmark said the trees are the better model and the additive model was only
easier to deploy. This closes that gap without a new runtime: LightGBM trains the
ensemble (a development dependency), and the *dumped* trees are flattened into
integer nodes -- split feature, integer threshold, child indices, and per-class
leaf contributions in millionths. Serving needs nothing but the existing Rust
binary, and the artifact keeps the determinism the rest of the system relies on.

Training needs the library; serving does not. When it is missing, training reports
`unavailable` and whatever model is already promoted keeps serving.
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
from runner_web.gam_ranker import PROMOTION_MARGIN, _incumbent_metrics, _targets, metrics
from runner_web.labels import barrier_contract
from runner_web.ranker import (
    CLASS_NAMES,
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    _load_groups,
    register_served_model,
)

MODEL_KIND = "integer_trees_barrier_v1"
ARTIFACT_SCHEMA = "stonks.integer_trees.v1"
SCALE = 1_000_000
SEED = 20260921
NUM_LEAVES = int(os.getenv("TREE_RANKER_LEAVES", "15"))
MIN_DATA_IN_LEAF = int(os.getenv("TREE_RANKER_MIN_LEAF", "40"))
ROUNDS = int(os.getenv("TREE_RANKER_ROUNDS", "120"))
LEARNING_RATE = float(os.getenv("TREE_RANKER_LEARNING_RATE", "0.05"))
# Keep the exported forest a size the runtime can walk cheaply per request.
MAX_TREES = int(os.getenv("TREE_RANKER_MAX_TREES", "600"))


def available() -> bool:
    """Whether the tree library is installed in this environment."""

    try:
        import lightgbm  # noqa: F401
    except Exception:
        return False
    return True


def train_booster(features: np.ndarray, targets: np.ndarray) -> Any:
    """Fit the ensemble; deterministic and single-threaded so runs agree."""

    import lightgbm as lgb

    dataset = lgb.Dataset(np.asarray(features, dtype=np.float64), label=targets)
    params = {
        "objective": "multiclass",
        "num_class": len(CLASS_NAMES),
        "num_leaves": NUM_LEAVES,
        "min_data_in_leaf": MIN_DATA_IN_LEAF,
        "learning_rate": LEARNING_RATE,
        "lambda_l2": 1.0,
        "feature_fraction": 1.0,
        "bagging_fraction": 1.0,
        "seed": SEED,
        "deterministic": True,
        "force_row_wise": True,
        "num_threads": 1,
        "verbose": -1,
    }
    return lgb.train(params, dataset, num_boost_round=ROUNDS)


def flatten_tree(structure: dict[str, Any], class_index: int) -> list[dict[str, Any]]:
    """One dumped tree as integer nodes: splits by index, leaves by class."""

    nodes: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> int:
        index = len(nodes)
        nodes.append({"feature": -1, "threshold": 0, "left": -1, "right": -1, "leaf": [0, 0, 0]})
        if "split_feature" not in node:
            leaf = [0, 0, 0]
            leaf[class_index] = int(round(float(node.get("leaf_value") or 0.0) * SCALE))
            nodes[index]["leaf"] = leaf
            return index
        nodes[index]["feature"] = int(node["split_feature"])
        # Floor, not round: the decision is `value <= threshold` for integer
        # features, and flooring is the only rounding that preserves it exactly.
        nodes[index]["threshold"] = int(math.floor(float(node.get("threshold") or 0.0)))
        nodes[index]["left"] = visit(node["left_child"])
        nodes[index]["right"] = visit(node["right_child"])
        return index

    visit(structure)
    return nodes


def integer_artifact(booster: Any, *, feature_names: list[str] | None = None) -> dict[str, Any]:
    """The whole forest as integers, with the label contract attached."""

    dump = booster.dump_model()
    classes = len(CLASS_NAMES)
    # The artifact names exactly the features the forest splits on.
    if feature_names is None:
        width = int(getattr(booster, "num_feature", lambda: len(FEATURE_NAMES))())
        feature_names = list(FEATURE_NAMES)[:width]
    trees: list[list[dict[str, Any]]] = []
    for index, info in enumerate(dump.get("tree_info") or []):
        if index >= MAX_TREES:
            break
        trees.append(flatten_tree(info["tree_structure"], index % classes))
    return {
        "schema": ARTIFACT_SCHEMA,
        "model_kind": MODEL_KIND,
        "feature_names": list(feature_names),
        "classes": list(CLASS_NAMES),
        "scale": SCALE,
        "intercept": [0] * classes,
        "trees": trees,
        "label_contract": barrier_contract(),
    }


def score_integers(artifact: dict[str, Any], features: list[int]) -> list[float]:
    """Walk every tree and sum the leaves, in model units (not scaled)."""

    classes = len(artifact.get("classes") or CLASS_NAMES)
    totals = [float(value) for value in artifact.get("intercept") or [0] * classes]
    for tree in artifact.get("trees") or []:
        if not tree:
            continue
        index = 0
        for _ in range(len(tree) + 1):
            node = tree[index]
            feature = int(node.get("feature", -1))
            if feature < 0:
                leaf = node.get("leaf") or [0] * classes
                for class_index in range(min(classes, len(leaf))):
                    totals[class_index] += float(leaf[class_index])
                break
            if feature >= len(features):
                break
            goes_left = features[feature] <= int(node["threshold"])
            index = int(node["left"]) if goes_left else int(node["right"])
            if index < 0 or index >= len(tree):
                break
    scale = float(artifact.get("scale") or 1)
    return [value / scale for value in totals]


def probabilities_from_scores(scores: list[float]) -> list[float]:
    largest = max(scores)
    exps = [float(np.exp(value - largest)) for value in scores]
    total = sum(exps) or 1.0
    return [value / total for value in exps]


def predict(artifact: dict[str, Any], features: list[int]) -> list[float]:
    """Class probabilities for one fixed-point feature vector."""

    return probabilities_from_scores(score_integers(artifact, features))


def tree_count(artifact: dict[str, Any]) -> int:
    return len(artifact.get("trees") or [])


def train_and_store(
    database: DatabaseConnection | None = None,
    *,
    horizon: str = "60m",
    maximum_groups: int = 200,
) -> dict[str, Any]:
    """Train, export, compare with the incumbent, and promote only if it wins."""

    if not available():
        return {"status": "unavailable", "reason": "lightgbm is not installed"}
    groups = _load_groups(horizon, maximum_groups=maximum_groups)
    if len(groups) < 6:
        return {"status": "insufficient", "groups": len(groups)}
    split = max(1, int(len(groups) * 0.8))
    train_groups, test_groups = groups[:split], groups[split:]
    if not test_groups:
        return {"status": "insufficient", "groups": len(groups)}
    features, targets = _targets(train_groups)
    test_features, test_targets = _targets(test_groups)
    booster = train_booster(features, targets)
    artifact = integer_artifact(booster)
    scored = np.asarray([predict(artifact, list(row)) for row in test_features])
    challenger = metrics(scored, test_targets)
    incumbent = _incumbent_metrics(test_groups)
    promoted = bool(
        incumbent.get("available")
        and challenger["log_loss"] < incumbent["log_loss"] - PROMOTION_MARGIN
        and challenger["log_loss"] < challenger["base_log_loss"]
    )
    moment = datetime.now(UTC).isoformat()
    model_id = f"trees-{moment.replace(':', '').replace('-', '')[:15]}"
    _store(database, model_id, artifact, challenger, incumbent, promoted, moment)
    return {
        "status": "trained",
        "model_id": model_id,
        "groups": len(groups),
        "rows": int(len(features)),
        "trees": tree_count(artifact),
        "promoted": promoted,
        "challenger": challenger,
        "incumbent": incumbent,
    }


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
        INSERT INTO tree_ranker_models(
            id,feature_schema_version,model_kind,artifact_json,metrics_json,
            training_rows,status,created_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO NOTHING
        """
    retire = "UPDATE tree_ranker_models SET status='retired' WHERE status='active' AND id<>?"
    if database is not None:
        database.execute(statement, payload)
        if promoted:
            database.execute(retire, (model_id,))
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
        return
    with connection() as db:
        db.execute(statement, payload)
        if promoted:
            db.execute(retire, (model_id,))
            register_served_model(
                model_id,
                MODEL_KIND,
                artifact,
                {"challenger": challenger, "incumbent": incumbent, "model_kind": MODEL_KIND},
                training_groups=int(challenger.get("rows") or 0),
                training_rows=int(challenger.get("rows") or 0),
                created_at=moment,
                connection=db,
            )


_ACTIVE_CACHE: tuple[float, dict[str, Any] | None] | None = None


def active_model(*, refresh: bool = False) -> dict[str, Any] | None:
    """The promoted tree ensemble, if one has earned it."""

    global _ACTIVE_CACHE
    moment = datetime.now(UTC).timestamp()
    if not refresh and _ACTIVE_CACHE and moment - _ACTIVE_CACHE[0] < 300:
        return _ACTIVE_CACHE[1]
    model: dict[str, Any] | None = None
    try:
        with connection() as database:
            row = database.execute(
                """
                SELECT id,artifact_json,metrics_json,created_at FROM tree_ranker_models
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
    """Console entry point: train the tree challenger and report the comparison."""

    import argparse

    parser = argparse.ArgumentParser(description="Train and export the integer tree ensemble")
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