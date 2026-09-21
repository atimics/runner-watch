"""Does nonlinearity earn its keep? The benchmark has to be able to say yes.

These tests use the ranker's fixed-point feature scale, because the additive
model's bin edges are integers: at unit scale the edges collapse and the additive
model would be judged on a degenerate model rather than a real one.
"""

from __future__ import annotations

import numpy as np
import pytest

from runner_web import tree_challenger as trees

FEATURE_SCALE = 1024
ROWS = 3000


def _dataset(kind: str, *, seed: int = 7):
    rng = np.random.default_rng(seed)
    features = (rng.normal(0, 1, (ROWS, 4)) * FEATURE_SCALE).round()
    if kind == "interaction":
        # Only the interaction of two features decides the class.
        signal = np.sign(features[:, 0] * features[:, 1])
        targets = np.where(signal > 0, 2, 0)
        targets = np.where(rng.random(ROWS) < 0.25, 1, targets)
    else:
        score = 2.0 * features[:, 0] - 1.5 * features[:, 1]
        targets = np.where(score > 600, 2, np.where(score < -600, 0, 1))
    return features, targets


requires_lightgbm = pytest.mark.skipif(
    not trees.available(), reason="lightgbm is a development dependency"
)


@requires_lightgbm
def test_trees_capture_an_interaction_the_additive_model_cannot():
    features, targets = _dataset("interaction")

    report = trees.compare(features, targets, boundary=int(ROWS * 0.8))

    assert report["status"] == "compared"
    assert report["verdict"] == "trees_win"
    # The additive model has no skill here: it cannot see the interaction.
    assert report["additive"]["log_loss"] >= report["base"]["log_loss"] - 0.05
    assert report["trees"]["log_loss"] < report["additive"]["log_loss"]
    assert report["log_loss_gap"] > 0


@requires_lightgbm
def test_the_report_always_carries_every_model_and_the_base_rate():
    features, targets = _dataset("additive")

    report = trees.compare(features, targets, boundary=int(ROWS * 0.8))

    for key in ("base", "additive", "trees"):
        assert report[key]["log_loss"] is not None
    assert report["base"]["log_loss"] == report["additive"]["base_log_loss"]
    assert report["verdict"] in {"trees_win", "additive_wins", "additive_holds"}
    assert report["holdout_rows"] == ROWS - int(ROWS * 0.8)


@requires_lightgbm
def test_the_benchmark_is_deterministic():
    features, targets = _dataset("additive", seed=13)

    first = trees.compare(features, targets, boundary=2000)
    second = trees.compare(features, targets, boundary=2000)

    assert first["trees"]["log_loss"] == second["trees"]["log_loss"]
    assert first["additive"]["log_loss"] == second["additive"]["log_loss"]


def test_a_missing_library_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(trees, "available", lambda: False)

    report = trees.compare(np.zeros((10, 2)), np.zeros(10))

    assert report == {"status": "unavailable", "reason": "lightgbm is not installed"}


@requires_lightgbm
def test_an_empty_side_of_the_split_is_refused():
    features, targets = _dataset("additive")

    report = trees.compare(features, targets, boundary=0)

    assert report["status"] == "insufficient"