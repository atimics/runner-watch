import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.kol import calls_for_ticker, publish_calls_for_scan
from runner_web.main import _pulse_data_uncached, ticker_detail_data
from runner_web.ranker import load_latest_model, promote_ranker, train_shadow_ranker
from runner_web.ranker_promotion import promotion_status
from tests.test_kol import _seed_prediction
from tests.test_ranker import _seed_ranker_data


def passing_metrics():
    split = {
        "groups": 16,
        "rows": 500,
        "selected_up_rate_ppm": 400_000,
        "baseline_selected_up_rate_ppm": 350_000,
        "precision_at_5_ppm": 300_000,
        "baseline_precision_at_5_ppm": 250_000,
        "mean_selected_return_bp": 100,
        "baseline_mean_selected_return_bp": 50,
        "up_brier_ppm": 150_000,
        "up_expected_calibration_error_ppm": 40_000,
    }
    return {"validation": dict(split), "test": dict(split)}


@pytest.mark.parametrize("split", ["validation", "test"])
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("groups", 15),
        ("rows", 499),
        ("selected_up_rate_ppm", 350_000),
        ("precision_at_5_ppm", 200_000),
        ("mean_selected_return_bp", 55),
        ("up_brier_ppm", 300_000),
        ("up_expected_calibration_error_ppm", 120_000),
        ("up_brier_ppm", -1),
        ("selected_up_rate_ppm", 1_000_001),
        ("rows", float("nan")),
        ("rows", float("inf")),
        ("rows", True),
        ("rows", None),
    ],
)
def test_each_evaluation_split_must_pass_all_gates(split, key, value) -> None:
    metrics = passing_metrics()
    metrics[split][key] = value
    result = promotion_status(metrics)
    assert result["eligible"] is False
    assert any(reason.startswith(split) for reason in result["reasons"])


def test_promotion_requires_complete_measured_evidence() -> None:
    for value in (None, {}, {"promotion": {"eligible": True}}, {"validation": {}}):
        assert promotion_status(value)["eligible"] is False
    assert promotion_status(passing_metrics())["eligible"] is True


def test_promotion_checks_evidence_and_replaces_the_active_model(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "promotion.db")
    init_db()
    _seed_ranker_data()
    result = train_shadow_ranker(min_groups=6, min_rows=24, epochs=20)
    model_id = result["model_id"]
    assert result["metrics"]["promotion"]["eligible"] is False
    with pytest.raises(ValueError, match="more evidence"):
        promote_ranker(model_id)
    assert load_latest_model().status == "shadow"
    with connection() as database:
        database.execute(
            "UPDATE ranker_models SET metrics_json=? WHERE id=?",
            (json.dumps(passing_metrics()), model_id),
        )
    promoted = promote_ranker(model_id)
    assert promoted["status"] == "active"
    assert promoted["promotion"]["approved_at"]
    assert load_latest_model().status == "active"
    with connection() as database:
        database.execute(
            """
            INSERT INTO ranker_models SELECT 'second',feature_schema_version,horizon,model_kind,
                weights_json,metrics_json,training_start,training_end,training_groups,
                training_rows,'shadow',created_at FROM ranker_models WHERE id=?
            """,
            (model_id,),
        )
    promote_ranker("second")
    with connection() as database:
        active = database.execute("SELECT id FROM ranker_models WHERE status='active'").fetchall()
    assert [row["id"] for row in active] == ["second"]


def test_shadow_predictions_keep_public_scores_on_the_baseline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "public.db")
    init_db()
    captured_at = datetime.now(UTC)
    _seed_prediction(
        "run-one",
        "snapshot-one",
        "SAFE",
        captured_at,
        probability_up=0.9,
        expected_return_pct=6.0,
    )
    with connection() as database:
        database.execute("UPDATE ranker_models SET status='shadow'")
    result = _pulse_data_uncached()
    row = next(row for row in result["rows"] if row["ticker"] == "SAFE")
    assert row["model_score"] is None
    assert row["model_rank"] is None
    assert row["directional_thesis"] is None
    assert row["score_components"]["market"] == row["baseline_score"]
    assert ticker_detail_data("SAFE")["directional_thesis"] is None
    publish_calls_for_scan("run-one", "model-one", at=captured_at)
    assert calls_for_ticker("SAFE") == []
    with connection() as database:
        database.execute("UPDATE ranker_models SET status='active'")
    assert _pulse_data_uncached()["rows"][0]["model_score"] is not None
