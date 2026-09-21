"""The additive challenger: integer tables, honest losses, no self-promotion.

The model has to be three things to be worth serving: additive (so its tables
export to the integer runtime), readable (so a "why" panel is real), and
unable to promote itself without beating the incumbent on held-out groups.
"""

from __future__ import annotations

import numpy as np
import pytest

from runner_web import db, gam_ranker
from runner_web.db import init_db
from tests.test_ranker import _seed_ranker_data


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "gam.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(gam_ranker, "_ACTIVE_CACHE", None)
    init_db()
    with db.connection() as handle:
        yield handle


def _learnable(rows: int = 800, columns: int = 4):
    rng = np.random.default_rng(5)
    features = rng.normal(0, 1000, (rows, columns)).round()
    # Up when the first feature is high, down when it is low.
    targets = np.where(features[:, 0] > 300, 2, np.where(features[:, 0] < -300, 0, 1))
    return features, targets


def test_bin_edges_are_deterministic_and_ordered():
    column = np.asarray([0, 10, 20, 30, 40, 50], dtype=np.float64)
    first = gam_ranker.quantile_edges(column, bins=4)
    assert first == gam_ranker.quantile_edges(column, bins=4)
    assert first == sorted(first)
    assert len(set(first)) == len(first)


def test_bin_lookup_puts_values_on_the_right_side_of_an_edge():
    edges = [10, 20, 30]
    assert gam_ranker.bin_index(5, edges) == 0
    assert gam_ranker.bin_index(10, edges) == 0  # at the edge stays in the lower bin
    assert gam_ranker.bin_index(11, edges) == 1
    assert gam_ranker.bin_index(999, edges) == 3  # beyond the last edge is the last bin


def test_the_model_learns_an_additive_signal_and_beats_the_base_rate():
    features, targets = _learnable()
    model = gam_ranker.build_tables(features, targets)
    artifact = gam_ranker.integer_artifact(model)

    scored = np.asarray([gam_ranker.predict(artifact, list(row)) for row in features])
    report = gam_ranker.metrics(scored, targets)

    assert report["log_loss"] < report["base_log_loss"]
    assert report["accuracy"] > report["base_accuracy"]
    # The informative feature's table separates the classes it was trained on.
    table = artifact["tables"][0]
    assert table[0][0] > table[0][2]  # low bins push down
    assert table[-1][2] > table[-1][0]  # high bins push up


def test_the_integer_artifact_matches_the_float_tables():
    features, targets = _learnable(rows=200)
    model = gam_ranker.build_tables(features, targets)
    artifact = gam_ranker.integer_artifact(model)
    vector = [int(value) for value in features[0]]

    scores = gam_ranker.score_integers(artifact, vector)
    expected = list(model["intercept"])
    for index, value in enumerate(vector):
        position = gam_ranker.bin_index(value, model["edges"][index])
        expected = [
            total + model["tables"][index][position][class_index]
            for class_index, total in enumerate(expected)
        ]
    assert scores == pytest.approx(expected, abs=1e-6)
    probabilities = gam_ranker.predict(artifact, vector)
    assert sum(probabilities) == pytest.approx(1.0)


def test_contributions_name_the_strongest_push_on_the_leading_class():
    features, targets = _learnable(rows=400)
    artifact = gam_ranker.integer_artifact(gam_ranker.build_tables(features, targets))
    vector = [3000, 0, 0, 0]

    rows = gam_ranker.contributions(artifact, vector, top=2)

    assert len(rows) == 2
    assert rows[0]["feature"] == "baseline_score"
    assert rows[0]["points"] > 0
    assert abs(rows[0]["points"]) >= abs(rows[1]["points"])


def test_a_challenger_without_an_incumbent_stays_in_shadow(database, monkeypatch):
    _seed_ranker_data(group_count=10, candidates=4)

    result = gam_ranker.train_and_store(database, maximum_groups=10)

    assert result["status"] == "trained"
    assert result["promoted"] is False
    assert result["incumbent"]["available"] is False
    with database as conn:
        row = dict(
            conn.execute("SELECT status,training_rows FROM gam_ranker_models").fetchone()
        )
    assert row["status"] == "shadow"
    assert row["training_rows"] > 0
    assert gam_ranker.active_model(refresh=True) is None


def test_a_challenger_that_does_not_beat_the_margin_is_not_promoted(database, monkeypatch):
    _seed_ranker_data(group_count=10, candidates=4)
    monkeypatch.setattr(gam_ranker, "PROMOTION_MARGIN", 10.0)
    # Pretend an incumbent exists and scored perfectly, so the margin cannot be met.
    monkeypatch.setattr(
        gam_ranker,
        "_incumbent_metrics",
        lambda _groups: {"available": True, "log_loss": 0.0, "model_id": "incumbent"},
    )

    result = gam_ranker.train_and_store(database, maximum_groups=10)

    assert result["promoted"] is False
    assert result["incumbent"]["available"] is True
    with database as conn:
        status = conn.execute("SELECT status FROM gam_ranker_models").fetchone()["status"]
    assert status == "shadow"


def test_a_challenger_that_beats_the_margin_is_promoted_and_served(database, monkeypatch):
    _seed_ranker_data(group_count=10, candidates=4)
    monkeypatch.setattr(
        gam_ranker,
        "_incumbent_metrics",
        lambda _groups: {"available": True, "log_loss": 5.0, "model_id": "incumbent"},
    )

    result = gam_ranker.train_and_store(database, maximum_groups=10)

    assert result["promoted"] is True
    database.commit()  # the served model is read on its own connection
    model = gam_ranker.active_model(refresh=True)
    assert model is not None
    assert model["artifact"]["schema"] == gam_ranker.ARTIFACT_SCHEMA
    assert model["artifact"]["label_contract"]["policy"].startswith("barriers.v1")
    assert model["metrics"]["incumbent"]["model_id"] == "incumbent"

def test_the_rust_runtime_serves_the_same_integer_tables(tmp_path, monkeypatch):
    """The export is only real if the deployed runtime agrees with it.

    Python scores the integer tables; the Rust binary scores the same artifact.
    They must land on the same probabilities to within one part per million, which
    is the whole point of keeping the artifact integer.
    """
    import json
    import subprocess
    from pathlib import Path

    binary = Path(__file__).parents[1] / "rust/stonks-ranker/target/debug/stonks-integer-ranker"
    if not binary.exists():
        pytest.skip("integer ranker binary is not built")

    features, targets = _learnable(rows=400)
    artifact = gam_ranker.integer_artifact(gam_ranker.build_tables(features, targets))
    vector = [int(value) for value in features[7]]

    response = subprocess.run(
        [str(binary)],
        input=json.dumps(
            {
                "command": "predict_gam",
                "artifact": artifact,
                "rows": [{"id": "one", "ticker": "ONE", "features": vector}],
            }
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    prediction = json.loads(response.stdout)["predictions"][0]
    python = gam_ranker.predict(artifact, vector)

    # Two parts per million: the runtime rounds its softmax to whole ppm.
    tolerance = 2e-6
    assert prediction["probability_down_ppm"] / 1e6 == pytest.approx(python[0], abs=tolerance)
    assert prediction["probability_timeout_ppm"] / 1e6 == pytest.approx(
        python[1], abs=tolerance
    )
    assert prediction["probability_up_ppm"] / 1e6 == pytest.approx(python[2], abs=tolerance)
    assert (
        prediction["probability_down_ppm"]
        + prediction["probability_timeout_ppm"]
        + prediction["probability_up_ppm"]
    ) == 1_000_000
