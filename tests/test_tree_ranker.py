"""The exported forest has to survive the trip into the runtime intact.

LightGBM trains it, the flattening turns it into integer nodes, and the Rust binary
walks those nodes. These tests pin the walk, the promotion rule, and the parity
between the trainer's own scorer and the deployed runtime.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from runner_web import db, tree_ranker
from runner_web.db import init_db

FEATURE_SCALE = 1024
ROWS = 2000

requires_lightgbm = pytest.mark.skipif(
    not tree_ranker.available(), reason="lightgbm is a development dependency"
)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "trees.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(tree_ranker, "_ACTIVE_CACHE", None)
    init_db()
    with db.connection() as handle:
        yield handle


def _dataset(*, seed: int = 7):
    rng = np.random.default_rng(seed)
    features = (rng.normal(0, 1, (ROWS, 4)) * FEATURE_SCALE).round()
    signal = np.sign(features[:, 0] * features[:, 1])
    targets = np.where(signal > 0, 2, 0)
    targets = np.where(rng.random(ROWS) < 0.25, 1, targets)
    return features, targets


@requires_lightgbm
def test_the_exported_forest_keeps_the_boosters_skill():
    features, targets = _dataset()
    boundary = int(ROWS * 0.8)

    artifact = tree_ranker.integer_artifact(
        tree_ranker.train_booster(features[:boundary], targets[:boundary])
    )
    scored = np.asarray([tree_ranker.predict(artifact, list(row)) for row in features[boundary:]])
    report = tree_ranker.metrics(scored, targets[boundary:])

    assert tree_ranker.tree_count(artifact) > 0
    assert report["log_loss"] < report["base_log_loss"]
    assert report["accuracy"] > report["base_accuracy"]


@requires_lightgbm
def test_the_export_is_integer_and_carries_the_contract():
    features, targets = _dataset()
    artifact = tree_ranker.integer_artifact(
        tree_ranker.train_booster(features[:1600], targets[:1600])
    )

    assert artifact["schema"] == tree_ranker.ARTIFACT_SCHEMA
    assert artifact["scale"] == tree_ranker.SCALE
    assert artifact["label_contract"]["policy"].startswith("barriers.v1")
    for tree in artifact["trees"]:
        for node in tree:
            assert isinstance(node["feature"], int)
            assert isinstance(node["threshold"], int)
            assert all(isinstance(value, int) for value in node["leaf"])


@requires_lightgbm
def test_the_runtime_walks_the_exported_forest_the_same_way():
    """Parity with the deployed binary, to within the ppm rounding of its softmax."""

    binary = Path(__file__).parents[1] / "rust/stonks-ranker/target/debug/stonks-integer-ranker"
    if not binary.exists():
        pytest.skip("integer ranker binary is not built")

    features, targets = _dataset()
    artifact = tree_ranker.integer_artifact(
        tree_ranker.train_booster(features[:1600], targets[:1600])
    )
    vectors = [[int(value) for value in row] for row in features[1700:1705]]

    response = subprocess.run(
        [str(binary)],
        input=json.dumps(
            {
                "command": "predict_trees",
                "artifact": artifact,
                "rows": [
                    {"id": f"row-{index}", "ticker": f"R{index}", "features": vector}
                    for index, vector in enumerate(vectors)
                ],
            }
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    predictions = json.loads(response.stdout)["predictions"]
    # The runtime's softmax is fixed-point (truncated series, whole-ppm output),
    # so a few parts per million of rounding is the contract, not drift.
    tolerance = 5e-6
    for prediction, vector in zip(predictions, vectors, strict=True):
        expected = tree_ranker.predict(artifact, vector)
        assert prediction["probability_down_ppm"] / 1e6 == pytest.approx(expected[0], abs=tolerance)
        assert prediction["probability_timeout_ppm"] / 1e6 == pytest.approx(
            expected[1], abs=tolerance
        )
        assert prediction["probability_up_ppm"] / 1e6 == pytest.approx(expected[2], abs=tolerance)


def _learnable_groups(groups: int = 40, per_group: int = 6) -> list[list[dict[str, object]]]:
    """Groups whose outcome a model can actually learn, for the promotion rule."""

    rng = np.random.default_rng(3)
    rows: list[list[dict[str, object]]] = []
    for group_index in range(groups):
        group = []
        for index in range(per_group):
            first = float(rng.normal(0, 1) * FEATURE_SCALE)
            second = float(rng.normal(0, 1) * FEATURE_SCALE)
            outcome = "up" if np.sign(first * second) > 0 else "down"
            group.append(
                {
                    "snapshot_id": f"{group_index}-{index}",
                    "scan_run_id": f"run-{group_index}",
                    "ticker": f"T{index}",
                    "run_captured_at": (
                        datetime(2026, 9, 1, 15, tzinfo=UTC) + timedelta(days=group_index)
                    ).isoformat(),
                    "expected_candidates": per_group,
                    "feature_vector": (int(first), int(second), 0, 0),
                    "score": 50.0,
                    "outcome": outcome,
                    "outcome_return": 1.0,
                    "training_origin": "live",
                }
            )
        rows.append(group)
    return rows


@requires_lightgbm
def test_a_tree_that_improves_stays_shadow_pending_prospective_evidence(database, monkeypatch):
    monkeypatch.setattr(tree_ranker, "_load_groups", lambda *_a, **_k: _learnable_groups())
    monkeypatch.setattr(
        tree_ranker,
        "_incumbent_metrics",
        lambda _groups: {"available": True, "log_loss": 5.0, "model_id": "incumbent"},
    )

    result = tree_ranker.train_and_store(database, maximum_groups=10)

    assert result["status"] == "trained"
    assert result["promoted"] is False
    assert result["candidate_improved"] is True
    assert result["trees"] > 0
    database.commit()
    assert tree_ranker.active_model(refresh=True) is None
    row = database.execute("SELECT artifact_json,status FROM tree_ranker_models").fetchone()
    assert row["status"] == "shadow"
    assert json.loads(row["artifact_json"])["schema"] == tree_ranker.ARTIFACT_SCHEMA


@requires_lightgbm
def test_a_tree_that_does_not_beat_the_margin_stays_in_shadow(database, monkeypatch):
    monkeypatch.setattr(tree_ranker, "_load_groups", lambda *_a, **_k: _learnable_groups())
    monkeypatch.setattr(
        tree_ranker,
        "_incumbent_metrics",
        lambda _groups: {"available": True, "log_loss": 0.0, "model_id": "incumbent"},
    )

    result = tree_ranker.train_and_store(database, maximum_groups=10)

    assert result["promoted"] is False
    assert tree_ranker.active_model(refresh=True) is None
    with database as conn:
        status = conn.execute("SELECT status FROM tree_ranker_models").fetchone()["status"]
    assert status == "shadow"


def test_a_missing_library_is_reported_not_guessed(monkeypatch):
    monkeypatch.setattr(tree_ranker, "available", lambda: False)

    assert tree_ranker.train_and_store() == {
        "status": "unavailable",
        "reason": "lightgbm is not installed",
    }


def test_the_walk_handles_a_short_feature_vector():
    artifact = {
        "schema": tree_ranker.ARTIFACT_SCHEMA,
        "classes": ["down", "timeout", "up"],
        "scale": tree_ranker.SCALE,
        "intercept": [0, 0, 0],
        "trees": [[{"feature": 3, "threshold": 0, "left": 1, "right": 1, "leaf": [0, 0, 0]}]],
    }

    # A row missing the feature it splits on contributes nothing rather than raising.
    assert tree_ranker.score_integers(artifact, [0]) == [0.0, 0.0, 0.0]


@requires_lightgbm
def test_a_research_forest_cannot_replace_the_served_incumbent(database, monkeypatch):
    """Serving one model at a time is what keeps the score meaning one thing."""

    from runner_web.ranker import load_latest_model, load_served_model, register_served_model

    # The incumbent is serving.
    register_served_model(
        "incumbent-logistic",
        "integer_multiclass_logistic_barrier_v6",
        {"weights": [[0, 0, 0]], "means": [0], "scales": [1], "bias": [0, 0, 0]},
        {"model_kind": "integer_multiclass_logistic_barrier_v6"},
        connection=database,
    )
    database.commit()
    assert load_served_model() is not None

    monkeypatch.setattr(tree_ranker, "_load_groups", lambda *_a, **_k: _learnable_groups())
    monkeypatch.setattr(
        tree_ranker,
        "_incumbent_metrics",
        lambda _groups: {"available": True, "log_loss": 5.0, "model_id": "incumbent-logistic"},
    )
    result = tree_ranker.train_and_store(database, maximum_groups=10)
    database.commit()

    assert result["promoted"] is False
    assert result["candidate_improved"] is True
    served = load_served_model()
    assert served is not None and served.id == "incumbent-logistic"
    assert load_latest_model() is None  # fixture deliberately has no valid logistic artifact
    statuses = {
        row["id"]: row["status"]
        for row in database.execute("SELECT id,status FROM ranker_models").fetchall()
    }
    assert result["model_id"] not in statuses
    assert statuses["incumbent-logistic"] == "active"


def test_the_runtime_command_follows_the_served_kind():
    """The prediction path runs the command the served artifact needs."""

    from runner_web import ranker

    def model(kind: str) -> ranker.RankerModel:
        return ranker.RankerModel(
            id="model",
            horizon="60m",
            artifact={},
            metrics={},
            status="active",
            kind=kind,
        )

    assert ranker._predict_command(model("integer_trees_barrier_v1"), []) == "predict_trees"
    assert ranker._predict_command(model("integer_additive_barrier_v1"), []) == "predict_gam"
    assert ranker._predict_command(model("integer_multiclass_logistic_barrier_v6"), []) == "predict"
