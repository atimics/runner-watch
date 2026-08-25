from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch

from runner_watch.sample_data import SAMPLE_SYMBOLS, SampleMarketData
from runner_web import db
from runner_web import main as web_main
from runner_web.db import connection, init_db
from runner_web.ranker import (
    FEATURE_NAMES,
    export_crl_dataset,
    load_latest_model,
    predict_and_store,
    train_shadow_ranker,
)


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
                    "stonks.ranker_features.v1",
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
                        snapshot_id,ticker,base_price,base_at,return_1h_pct,updated_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id,
                        f"T{candidate}",
                        1.0 + candidate,
                        captured.isoformat(),
                        momentum * 3.0,
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
    model = load_latest_model()
    assert model is not None
    assert len(model.weights) == len(FEATURE_NAMES)

    prediction = predict_and_store("run-7", model)
    assert prediction["predicted"] is True
    with connection() as database:
        prediction_count = database.execute(
            "SELECT COUNT(*) FROM ranker_predictions WHERE model_id=?",
            (model.id,),
        ).fetchone()[0]
    assert prediction_count == 4

    destination = tmp_path / "stonks-crl.csv"
    exported = export_crl_dataset(destination)
    assert exported["groups"] == 8
    with destination.open(newline="", encoding="utf-8") as source:
        rows = list(csv.reader(source))
    assert rows[0][:5] == ["split", "group_id", "state_hash", "action_id", "target"]
    assert len(rows[0]) == 5 + len(FEATURE_NAMES)
    assert len(rows) == 1 + 8 * 4


def test_web_scan_saves_one_complete_candidate_group(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "web-scan.db")
    init_db()
    sample_now = datetime.now(UTC)
    provider = SampleMarketData(sample_now)
    monkeypatch.setattr(web_main, "recording_market_data", lambda batch_size=60: provider)
    monkeypatch.setattr(
        web_main,
        "penny_runner_universe",
        lambda **kwargs: ([SimpleNamespace(symbol=symbol) for symbol in SAMPLE_SYMBOLS], []),
    )
    web_main.SCAN_CACHE.clear()

    result = web_main.run_scan("penny")
    with connection() as database:
        run = database.execute("SELECT * FROM scan_runs").fetchone()
        snapshots = database.execute(
            "SELECT * FROM scan_snapshots ORDER BY baseline_rank"
        ).fetchall()
    assert result["scan_run_id"] == run["id"]
    assert run["candidate_rows"] == len(snapshots)
    assert len(snapshots) == result["ranked_candidates"]
    assert len(snapshots) > 0
    assert snapshots[0]["range_position"] is not None
    assert snapshots[0]["scan_run_id"] == run["id"]
