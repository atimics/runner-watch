"""Live-path regressions: unknown outcomes, probability units and migration repairs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from runner_web import db, gam_ranker, main, outcomes, ranker
from runner_web.db import connection, init_db
from tests.test_ranker import _seed_ranker_data
from tests.test_scoring_contracts import activity


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "integrity.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    init_db()
    _seed_ranker_data(group_count=1, candidates=3)
    ranker._backfill_recent_training_examples(2)
    with connection() as handle:
        yield handle


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), True, "invalid"])
def test_unknown_returns_stay_null_in_compact_training_rows(database, value):
    ranker.sync_training_outcome(database, "snapshot-0-0", "up", value, "2026-09-21")
    assert (
        database.execute(
            "SELECT outcome_return_bp FROM ranker_training_examples "
            "WHERE snapshot_id='snapshot-0-0'"
        ).fetchone()[0]
        is None
    )
    with pytest.raises(ValueError, match="observed terminal"):
        ranker._required_return_bp(value)


def test_a_real_zero_return_is_observed_not_missing(database):
    ranker.sync_training_outcome(database, "snapshot-0-0", "timeout", 0, "2026-09-21")
    assert (
        database.execute(
            "SELECT outcome_return_bp FROM ranker_training_examples "
            "WHERE snapshot_id='snapshot-0-0'"
        ).fetchone()[0]
        == 0
    )


def test_migration_repairs_legacy_labels_without_destroying_genuine_zero(database):
    database.execute(
        "UPDATE scan_outcomes SET barrier_ambiguous=1 WHERE snapshot_id='snapshot-0-0'"
    )
    database.execute(
        "UPDATE scan_outcomes SET return_60m_pct=NULL WHERE snapshot_id='snapshot-0-0'"
    )
    database.execute("UPDATE scan_outcomes SET return_60m_pct=0 WHERE snapshot_id='snapshot-0-1'")
    database.execute("UPDATE ranker_training_examples SET outcome_return_bp=0")
    database.execute(
        "UPDATE ranker_training_examples SET training_origin='historical_replay', "
        "barrier_resolution=NULL WHERE snapshot_id='snapshot-0-2'"
    )
    db._migration_082_scoring_integrity(database)
    rows = {
        r["snapshot_id"]: dict(r)
        for r in database.execute(
            "SELECT snapshot_id,barrier_resolution,outcome_return_bp FROM ranker_training_examples"
        )
    }
    assert rows["snapshot-0-0"]["barrier_resolution"] == "ambiguous"
    assert rows["snapshot-0-0"]["outcome_return_bp"] is None
    assert rows["snapshot-0-1"]["outcome_return_bp"] == 0
    assert rows["snapshot-0-2"]["barrier_resolution"] == "unverified_legacy"
    assert len(rows) == 3


def test_terminal_return_is_retried_after_barrier_was_already_resolved(database, monkeypatch):
    now = datetime(2026, 9, 21, 15, tzinfo=UTC)
    base = now - timedelta(minutes=90)
    database.execute(
        "UPDATE scan_snapshots SET captured_at=?,quote_time=? WHERE id='snapshot-0-0'",
        (base.isoformat(), base.isoformat()),
    )
    database.execute(
        "UPDATE scan_outcomes SET base_at=?,base_price=100,barrier_label='up', "
        "barrier_resolution='resolved',return_60m_pct=NULL,return_1h_pct=NULL "
        "WHERE snapshot_id='snapshot-0-0'",
        (base.isoformat(),),
    )
    ranker.sync_training_outcome(database, "snapshot-0-0", "up", None, now.isoformat())
    database.commit()
    bars = [(base + timedelta(minutes=5), 110.0, 99.0, 109.0)]
    monkeypatch.setattr(outcomes, "_bar_prices", lambda *_args, **_kwargs: {"T0": bars})
    first = outcomes.refresh_scan_outcomes(at=now)
    assert first["deferred"] == 1
    bars.append((base + timedelta(minutes=60), 106.0, 99.0, 105.0))
    outcomes.refresh_scan_outcomes(at=now + timedelta(minutes=31))
    row = database.execute(
        "SELECT barrier_label,outcome_return_bp,barrier_resolution "
        "FROM ranker_training_examples WHERE snapshot_id='snapshot-0-0'"
    ).fetchone()
    assert row["barrier_label"] == "up"
    assert row["outcome_return_bp"] == 500
    assert row["barrier_resolution"] == "resolved"


def test_incumbent_comparison_decodes_integer_probabilities_and_matches_ids(monkeypatch):
    model = SimpleNamespace(id="incumbent", artifact={}, kind="integer_trees_barrier_v1")
    monkeypatch.setattr(gam_ranker, "load_served_model", lambda: model)
    requests = []

    def runtime(request):
        requests.append(request)
        return {
            "predictions": [
                {
                    "id": "a",
                    "probability_down_ppm": 100_000,
                    "probability_timeout_ppm": 200_000,
                    "probability_up_ppm": 700_000,
                }
            ]
        }

    monkeypatch.setattr(gam_ranker, "_run_rust", runtime)
    group = [[{"snapshot_id": "a", "ticker": "A", "feature_vector": (1, 2), "outcome": "up"}]]
    report = gam_ranker._incumbent_metrics(group)
    assert report["available"] is True
    assert report["log_loss"] == pytest.approx(-np.log(0.7), abs=1e-6)
    assert requests[0]["command"] == "predict_trees"
    monkeypatch.setattr(gam_ranker, "_run_rust", lambda _: {"predictions": []})
    assert gam_ranker._incumbent_metrics(group)["reason"] == "prediction_batch_mismatch"


def test_calibration_is_reliability_not_per_example_absolute_error():
    probabilities = np.asarray([[0.25, 0.0, 0.75]] * 4)
    targets = np.asarray([2, 2, 2, 0])
    report = gam_ranker.metrics(probabilities, targets, prior_targets=np.asarray([0, 0, 0, 2]))
    assert report["calibration_gap"] == 0
    assert report["class_calibration_gap"]["up"] == 0
    assert report["base_accuracy"] == 0.25
    assert report["base_rate_basis"] == "training"
    assert report["base_log_loss"] > 1
    with pytest.raises(ValueError, match="normalized"):
        gam_ranker.metrics(np.zeros((4, 3)), targets)


def test_attention_does_not_change_when_only_model_direction_or_engagement_changes():
    stamp = "2026-09-21T15:00:00+00:00"
    snapshot = {
        "id": "s",
        "ticker": "A",
        "price": 5,
        "quote_time": stamp,
        "captured_at": stamp,
        "rug_score": 0,
        "trade_state": "WATCH",
        **activity(),
    }
    base = {
        "score_as_of": stamp,
        "filings_by_ticker": {},
        "market_events_by_ticker": {},
        "community": {},
        "predictions": {},
    }
    first = main._pulse_snapshot_score(snapshot, base, include_trace=True)
    forecast = {
        "model_id": "m",
        "score": 90,
        "probability_up": 0.9,
        "probability_down": 0.02,
        "probability_timeout": 0.08,
        "expected_return_pct": 7.12,
    }
    second = main._pulse_snapshot_score(
        snapshot,
        {
            **base,
            "predictions": {"s": forecast},
            "community": {"A": {"call_count": 1000, "comment_count": 1000}},
        },
        include_trace=True,
    )
    assert first["score"] == second["score"] == 46
    assert second["forecast"]["probability_up"] == 0.9
    assert second["score_unit"] == "heuristic_points"
    assert second["eligibility"]["eligible"] is True
    assert "0 attention points" in second["score_trace"]["community"][-1]["value"].lower()


def test_public_projection_does_not_drop_scoring_contract(monkeypatch):
    row = {
        "ticker": "A",
        "score": 80,
        "score_policy": "attention-activity-v3",
        "score_unit": "heuristic_points",
        "feature_as_of": "then",
        "quote_as_of": "then",
        "computed_at": "now",
        "eligibility": {"state": "blocked", "blocked": True},
        "forecast": {"probability_up": 0.2},
        "attention_urgent": True,
    }
    monkeypatch.setattr(main, "pulse_data", lambda **_: {"rows": [row]})
    public = main._public_pulse_data()["rows"][0]
    assert public == row
    json.dumps(public, allow_nan=False)


def test_python_rejects_an_old_binary_that_silently_resplits(monkeypatch):
    from tests.test_scoring_contracts import groups

    samples = groups(count=20, minutes=120)
    for group in samples:
        group[:] = [
            {
                **group[0],
                "outcome": label,
                "feature_vector": (0,) * len(ranker.FEATURE_NAMES),
                "outcome_return": 0,
                "score": 0,
                "ticker": label,
            }
            for label in ("down", "timeout", "up")
        ]
    monkeypatch.setattr(ranker, "_load_groups", lambda *_a, **_k: samples)
    requests = []

    def old_runtime(request, **_kwargs):
        requests.append(request)
        return {"artifact": {}, "metrics": {"split": "legacy_fractional_split_unpurged"}}

    monkeypatch.setattr(ranker, "_run_rust", old_runtime)
    with pytest.raises(RuntimeError, match="preserve the explicit"):
        ranker.train_shadow_ranker(min_groups=6, min_rows=18, epochs=1)
    assert requests[0]["train_group_count"] == 16
    assert requests[0]["validation_group_count"] == 2
