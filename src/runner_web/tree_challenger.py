"""Does nonlinearity earn its keep? Benchmark the additive model against trees.

The additive (GAM) challenger is served from integer tables, which is why it is
cheap to deploy. The open question the review left is whether that shape is
leaving accuracy on the table: if the barrier question depends on interactions
(volume *and* extension, say), a tree ensemble can capture what an additive model
structurally cannot.

This module answers that question with the real library -- LightGBM, a development
dependency, so the benchmark runs in CI without shipping a training-only
dependency to production. It trains on the same purged groups and the same
chronological split as the additive model, scores all of them with the same proper
losses, and states a verdict. Nothing here is exported or served: if trees win,
the next decision is how to export them (Treelite or ONNX beside the integer
runtime), which is deliberately not smuggled into this comparison.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import numpy as np

from runner_web.gam_ranker import (
    PROMOTION_MARGIN,
    _incumbent_metrics,
    _targets,
    build_tables,
    integer_artifact,
    metrics,
    predict,
)
from runner_web.ranker import _load_groups

SEED = 20260921
NUM_LEAVES = int(os.getenv("TREE_CHALLENGER_LEAVES", "15"))
MIN_CHILD_SAMPLES = int(os.getenv("TREE_CHALLENGER_MIN_CHILD", "40"))
ROUNDS = int(os.getenv("TREE_CHALLENGER_ROUNDS", "120"))
LEARNING_RATE = float(os.getenv("TREE_CHALLENGER_LEARNING_RATE", "0.05"))


def available() -> bool:
    """Whether the tree library is installed in this environment."""

    try:
        import lightgbm  # noqa: F401
    except Exception:
        return False
    return True


def train_trees(features: np.ndarray, targets: np.ndarray) -> Any:
    """A small, deterministic, regularised multiclass booster.

    The native API is used rather than the scikit-learn wrapper so the benchmark
    needs one dependency, not two.
    """

    import lightgbm as lgb

    dataset = lgb.Dataset(np.asarray(features, dtype=np.float64), label=targets)
    params = {
        "objective": "multiclass",
        "num_class": 3,
        "num_leaves": NUM_LEAVES,
        "min_data_in_leaf": MIN_CHILD_SAMPLES,
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


def tree_probabilities(model: Any, features: np.ndarray) -> np.ndarray:
    """Probabilities in the same class order the metrics expect."""

    probabilities = np.asarray(
        model.predict(np.asarray(features, dtype=np.float64)), dtype=np.float64
    )
    if probabilities.ndim != 2 or probabilities.shape[1] != 3:
        raise ValueError("the booster did not model three classes")
    return probabilities


def compare(
    features: np.ndarray,
    targets: np.ndarray,
    *,
    boundary: int | None = None,
) -> dict[str, Any]:
    """Score the base rate, the additive model and the trees on one hold-out.

    ``boundary`` is the chronological split: everything before it trains, after it
    is scored. Features must be the fixed-point vectors the ranker uses, since the
    additive model's bin edges are integers.
    """

    if not available():
        return {"status": "unavailable", "reason": "lightgbm is not installed"}
    split_at = boundary if boundary is not None else max(1, int(len(features) * 0.8))
    train_x, test_x = features[:split_at], features[split_at:]
    train_y, targets_ = targets[:split_at], targets[split_at:]
    test_y = targets_
    if len(test_x) == 0 or len(train_x) == 0:
        return {"status": "insufficient", "rows": int(len(features))}

    additive_artifact = integer_artifact(build_tables(train_x, train_y))
    additive_scores = np.asarray([predict(additive_artifact, list(row)) for row in test_x])
    additive = metrics(additive_scores, test_y, prior_targets=train_y)

    trees = metrics(
        tree_probabilities(train_trees(train_x, train_y), test_x), test_y, prior_targets=train_y
    )

    verdict = "additive_holds"
    if trees["log_loss"] < additive["log_loss"] - PROMOTION_MARGIN:
        verdict = "trees_win"
    elif additive["log_loss"] < trees["log_loss"] - PROMOTION_MARGIN:
        verdict = "additive_wins"
    return {
        "status": "compared",
        "rows": int(len(features)),
        "holdout_rows": int(len(test_x)),
        "base": {
            "log_loss": additive["base_log_loss"],
            "accuracy": additive["base_accuracy"],
        },
        "additive": additive,
        "trees": trees,
        "margin": PROMOTION_MARGIN,
        "log_loss_gap": round(additive["log_loss"] - trees["log_loss"], 6),
        "verdict": verdict,
    }


def benchmark(*, horizon: str = "60m", maximum_groups: int = 200) -> dict[str, Any]:
    """The same comparison on the groups the incumbent trains from."""

    groups = _load_groups(horizon, maximum_groups=maximum_groups)
    if len(groups) < 6:
        return {"status": "insufficient", "groups": len(groups)}
    from runner_web.replay import purged_chronological_split

    split = purged_chronological_split(groups)
    train_groups, test_groups = split["train"], split["test"]
    if not all(split[key] for key in ("train", "validation", "test")):
        return {"status": "insufficient", "groups": len(groups), "split_receipt": split["receipt"]}
    features, targets = _targets(train_groups)
    test_features, test_targets = _targets(test_groups)
    if not len(test_features):
        return {"status": "insufficient", "groups": len(groups)}
    report = compare(
        np.concatenate([features, test_features]),
        np.concatenate([targets, test_targets]),
        boundary=len(features),
    )
    if report.get("status") != "compared":
        return report
    report["incumbent"] = _incumbent_metrics(test_groups)
    report["groups"] = len(groups)
    report["split_receipt"] = split["receipt"]
    report["generated_at"] = datetime.now(UTC).isoformat()
    return report


def main(argv: list[str] | None = None) -> int:
    """Console entry point: run the benchmark and print the verdict."""

    import argparse
    import json

    parser = argparse.ArgumentParser(description="Benchmark trees against the additive model")
    parser.parse_args(argv)
    print(json.dumps(benchmark(), separators=(",", ":")))
    return 0
