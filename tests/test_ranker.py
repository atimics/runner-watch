from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch

from runner_web import db
from runner_web import main as web_main
from runner_web.db import connection, init_db
from runner_web.ranker import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    _load_groups,
    export_crl_dataset,
    feature_vector,
    load_latest_model,
    predict_and_store,
    train_shadow_ranker,
)
from tests.fake_market_data import FAKE_SYMBOLS, FakeMarketData


def test_chart_structure_fields_are_ranker_features() -> None:
    row = {
        "opening_range_position": 1.25,
        "opening_range_breakout_pct": 2.5,
        "support_distance_pct": 1.1,
        "support_strength": 0.75,
        "resistance_distance_pct": 3.2,
        "resistance_strength": 0.5,
        "fib_retracement_pct": 61.8,
        "fib_level_distance_pct": 0.2,
        "structure_available": 1,
        "fibonacci_available": 1,
    }

    vector = feature_vector(row)
    values = dict(zip(FEATURE_NAMES, vector, strict=True))

    assert values["opening_range_position"] == 1_250
    assert values["support_strength"] == 750
    assert values["fib_retracement_pct"] == 61_800
    assert values["structure_missing"] == 0
    assert values["fibonacci_missing"] == 0


def _seed_ranker_data(group_count: int = 8, candidates: int = 4) -> None:
    start = datetime(2026, 8, 1, 14, tzinfo=UTC)
    with connection() as database:
        for group_index in range(group_count):
            captured = start + timedelta(hours=group_index)
            run_id = f"run-{group_index}"
            database.execute(
                """
                INSERT INTO scan_runs(
                    id,mode,label,feature_schema_version,requested_symbols,liquid_symbols,
                    scanned_symbols,candidate_rows,failed_symbols_json,warnings_json,
                    started_at,finished_at,captured_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    "penny",
                    "Penny stocks",
                    FEATURE_SCHEMA_VERSION,
                    candidates,
                    candidates,
                    candidates,
                    candidates,
                    "[]",
                    "[]",
                    captured.isoformat(),
                    captured.isoformat(),
                    captured.isoformat(),
                ),
            )
            for candidate in range(candidates):
                snapshot_id = f"snapshot-{group_index}-{candidate}"
                momentum = float(candidate + group_index % 2)
                database.execute(
                    """
                    INSERT INTO scan_snapshots(
                        id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                        momentum_15m_pct,relative_volume,recent_relative_volume,
                        breakout_pct,dollar_volume,quote_time,signals_json,risks_json,
                        captured_at,scan_run_id,baseline_rank,range_position,stale_minutes,
                        session_volume,average_volume,average_dollar_volume
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id,
                        f"T{candidate}",
                        float(candidates - candidate),
                        "WATCH",
                        "REGULAR",
                        1.0 + candidate,
                        momentum,
                        momentum,
                        momentum * 2,
                        1.0 + candidate,
                        1.0 + candidate,
                        momentum,
                        100_000.0 + candidate * 10_000,
                        captured.isoformat(),
                        "[]",
                        "[]",
                        captured.isoformat(),
                        run_id,
                        candidate + 1,
                        0.5 + candidate * 0.1,
                        0.0,
                        100_000,
                        100_000,
                        1_000_000.0,
                    ),
                )
                database.execute(
                    """
                    INSERT INTO scan_outcomes(
                        snapshot_id,ticker,base_price,base_at,barrier_label,
                        return_60m_pct,updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id,
                        f"T{candidate}",
                        1.0 + candidate,
                        captured.isoformat(),
                        ("down", "timeout", "up", "up")[candidate],
                        (-4.0, 0.5, 8.0, 9.0)[candidate],
                        (captured + timedelta(hours=1)).isoformat(),
                    ),
                )


def test_shadow_ranker_trains_predicts_and_exports_crl(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ranker.db")
    init_db()
    _seed_ranker_data()

    trained = train_shadow_ranker(min_groups=6, min_rows=24, epochs=120)
    assert trained["trained"] is True
    assert trained["integer_only"] is True
    assert trained["metrics"]["validation"]["groups"] == 1.0
    assert trained["metrics"]["test"]["groups"] == 1.0
    assert set(trained["metrics"]["validation"]["brier_ppm"]) == {
        "down",
        "timeout",
        "up",
    }
    assert set(trained["metrics"]["validation"]["expected_calibration_error_ppm"]) == {
        "down",
        "timeout",
        "up",
    }
    assert trained["metrics"]["training_data"]["groups"] == {"live": 8}
    training_control = trained["metrics"]["training_control"]
    assert training_control["requested_epochs"] == 120
    assert training_control["trained_epochs"] <= 120
    assert training_control["best_epoch"] <= training_control["trained_epochs"]
    assert training_control["validation_checks"] >= 1
    assert training_control["validation_log_loss_micros"] > 0
    model = load_latest_model()
    assert model is not None
    assert len(model.weights) == len(FEATURE_NAMES)
    assert model.artifact["schema"] == "stonks.integer_ranker.v1"
    assert model.artifact["feature_scale"] == 1_000
    assert all(isinstance(value, int) for row in model.weights for value in row)

    replayed = train_shadow_ranker(min_groups=6, min_rows=24, epochs=120)
    assert replayed["model_id"] == trained["model_id"]

    prediction = predict_and_store("run-7", model)
    assert prediction["predicted"] is True
    with connection() as database:
        prediction_count = database.execute(
            "SELECT COUNT(*) FROM ranker_predictions WHERE model_id=?",
            (model.id,),
        ).fetchone()[0]
        probabilities = database.execute(
            """
            SELECT probability_up,probability_down,probability_timeout,score
            FROM ranker_predictions WHERE model_id=?
            """,
            (model.id,),
        ).fetchall()
    assert prediction_count == 4
    assert all(0 <= row["score"] <= 100 for row in probabilities)
    assert all(
        abs(
            row["probability_up"]
            + row["probability_down"]
            + row["probability_timeout"]
            - 1
        )
        < 1e-9
        for row in probabilities
    )

    destination = tmp_path / "stonks-crl.csv"
    exported = export_crl_dataset(destination)
    assert exported["groups"] == 8
    with destination.open(newline="", encoding="utf-8") as source:
        rows = list(csv.reader(source))
    assert rows[0][:5] == ["split", "group_id", "state_hash", "action_id", "target"]
    assert len(rows[0]) == 5 + len(FEATURE_NAMES)
    assert len(rows) == 1 + 8 * 4


def test_ranker_compacts_and_bounds_legacy_training_rows(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "bounded-ranker.db")
    init_db()
    _seed_ranker_data(group_count=12, candidates=4)

    groups = _load_groups("60m", maximum_groups=5)

    with connection() as database:
        compact_rows = database.execute(
            "SELECT COUNT(*) FROM ranker_training_examples"
        ).fetchone()[0]
    assert len(groups) == 5
    assert sum(len(group) for group in groups) == 20
    assert compact_rows == 20


def test_web_scan_saves_one_complete_candidate_group(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "web-scan.db")
    init_db()
    sample_now = datetime.now(UTC)
    provider = FakeMarketData(sample_now)
    monkeypatch.setattr(web_main, "recording_market_data", lambda batch_size=60: provider)
    monkeypatch.setattr(
        web_main,
        "penny_runner_universe",
        lambda **kwargs: ([SimpleNamespace(symbol=symbol) for symbol in FAKE_SYMBOLS], []),
    )
    web_main.SCAN_CACHE.clear()

    result = web_main.run_scan("penny")
    with connection() as database:
        run = database.execute("SELECT * FROM scan_runs").fetchone()
        snapshots = database.execute(
            "SELECT * FROM scan_snapshots ORDER BY baseline_rank"
        ).fetchall()
        training_examples = database.execute(
            "SELECT * FROM ranker_training_examples ORDER BY ticker"
        ).fetchall()
        pulse_entries = database.execute("SELECT * FROM pulse_entries").fetchall()
    assert result["scan_run_id"] == run["id"]
    assert run["candidate_rows"] == len(snapshots)
    assert len(snapshots) == result["ranked_candidates"]
    assert len(snapshots) > 0
    assert snapshots[0]["range_position"] is not None
    assert snapshots[0]["opening_range_position"] is not None
    assert snapshots[0]["structure_available"] == 1
    assert snapshots[0]["scan_run_id"] == run["id"]
    assert len(training_examples) == len(snapshots)
    assert len(pulse_entries) == len(snapshots)
    assert len(json.loads(training_examples[0]["feature_vector_json"])) == len(FEATURE_NAMES)
