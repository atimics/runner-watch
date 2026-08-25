from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.kol import (
    calls_for_ticker,
    predictor_scorecards,
    publish_calls_for_scan,
    refresh_kol_calls,
)
from runner_web.main import pulse_data, ticker_detail_data


def _seed_prediction(
    run_id: str,
    snapshot_id: str,
    ticker: str,
    captured_at: datetime,
    *,
    probability_up: float,
    expected_return_pct: float,
    price: float = 10.0,
    rank: int = 1,
) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT OR IGNORE INTO ranker_models(
                id,feature_schema_version,horizon,model_kind,weights_json,metrics_json,
                training_start,training_end,training_groups,training_rows,status,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "model-one",
                "test",
                "60m",
                "test",
                "{}",
                "{}",
                captured_at.isoformat(),
                captured_at.isoformat(),
                1,
                1,
                "active",
                captured_at.isoformat(),
            ),
        )
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
                "test",
                1,
                1,
                1,
                1,
                "[]",
                "[]",
                captured_at.isoformat(),
                captured_at.isoformat(),
                captured_at.isoformat(),
            ),
        )
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,breakout_pct,dollar_volume,quote_time,signals_json,
                risks_json,captured_at,scan_run_id,baseline_rank
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                ticker,
                70.0,
                "BUILDING",
                "REGULAR",
                price,
                4.0,
                1.0,
                2.0,
                0.0,
                1_000_000.0,
                captured_at.isoformat(),
                "[]",
                "[]",
                captured_at.isoformat(),
                run_id,
                rank,
            ),
        )
        database.execute(
            """
            INSERT INTO ranker_predictions(
                snapshot_id,model_id,score,rank,created_at,probability_up,
                probability_down,probability_timeout,expected_return_pct
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                "model-one",
                probability_up * 100,
                rank,
                captured_at.isoformat(),
                probability_up,
                0.2,
                0.8 - probability_up,
                expected_return_pct,
            ),
        )


def test_ranker_prediction_becomes_one_immutable_flash_call(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "kol.db")
    init_db()
    captured_at = datetime(2026, 8, 25, 15, tzinfo=UTC)
    _seed_prediction(
        "run-one",
        "snapshot-one",
        "RUNR",
        captured_at,
        probability_up=0.7,
        expected_return_pct=2.5,
    )

    first = publish_calls_for_scan("run-one", "model-one")
    second = publish_calls_for_scan("run-one", "model-one")

    calls = calls_for_ticker("RUNR")
    assert first["calls_created"] == 1
    assert second["calls_created"] == 0
    assert len(calls) == 1
    assert calls[0]["emoji"] == "⚡"
    assert calls[0]["slot"] == "flash"
    assert calls[0]["ladder_position"] == 1
    assert calls[0]["inference_model"] == "z-ai/glm-5.3"
    assert calls[0]["inference_model_label"] == "GLM 5.3"
    assert calls[0]["signal_model_id"] == "model-one"
    assert calls[0]["authorship"] == "deterministic_signal_policy"
    assert calls[0]["entry_price"] == 10.0
    assert calls[0]["confidence"] == 0.7
    assert calls[0]["status"] == "active"
    assert pulse_data()["rows"][0]["kol_calls"][0]["emoji"] == "⚡"
    assert ticker_detail_data("RUNR")["kol_calls"][0]["id"] == calls[0]["id"]
    with connection() as database:
        events = database.execute(
            "SELECT event_type FROM kol_call_events WHERE call_id=?",
            (calls[0]["id"],),
        ).fetchall()
        database.execute(
            "UPDATE kol_predictors SET inference_model='future/model' WHERE id='kol-flash'"
        )
    assert [row["event_type"] for row in events] == ["called"]
    frozen_call = calls_for_ticker("RUNR")[0]
    assert frozen_call["inference_model"] == "z-ai/glm-5.3"
    assert frozen_call["inference_model_label"] == "GLM 5.3"


def test_fixed_outcome_closes_call_and_updates_scorecard(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "kol-outcome.db")
    init_db()
    captured_at = datetime(2026, 8, 25, 15, tzinfo=UTC)
    _seed_prediction(
        "run-one",
        "snapshot-one",
        "WINR",
        captured_at,
        probability_up=0.75,
        expected_return_pct=3.0,
    )
    publish_calls_for_scan("run-one", "model-one")
    observed_at = captured_at + timedelta(minutes=20)
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_outcomes(
                snapshot_id,ticker,base_price,base_at,barrier_label,barrier_hit_at,
                return_60m_pct,max_favorable_pct,max_adverse_pct,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "snapshot-one",
                "WINR",
                10.0,
                captured_at.isoformat(),
                "up",
                observed_at.isoformat(),
                8.4,
                9.0,
                -1.0,
                observed_at.isoformat(),
            ),
        )

    result = refresh_kol_calls(observed_at, latest_prices={"WINR": 10.8})
    call = calls_for_ticker("WINR")[0]
    scorecard = predictor_scorecards()[0]

    assert result["closed"] == 1
    assert call["status"] == "won"
    assert call["benchmark_label"] == "up"
    assert call["realized_return_pct"] == 8.0
    assert call["net_return_pct"] == 7.5
    assert call["paper_pnl"] == 75.0
    assert scorecard["hit_rate"] == 1.0
    assert scorecard["paper_pnl"] == 75.0


def test_archived_bar_closes_a_live_run_before_the_slower_outcome_job(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "kol-live-barrier.db")
    init_db()
    captured_at = datetime(2026, 8, 25, 15, tzinfo=UTC)
    _seed_prediction(
        "run-one",
        "snapshot-one",
        "FAST",
        captured_at,
        probability_up=0.72,
        expected_return_pct=2.8,
    )
    publish_calls_for_scan("run-one", "model-one")
    bar_at = captured_at + timedelta(minutes=5)
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,open,high,low,close,volume,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "yahoo",
                "FAST",
                "5m",
                bar_at.isoformat(),
                10.0,
                10.9,
                9.9,
                10.7,
                100_000,
                bar_at.isoformat(),
                bar_at.isoformat(),
            ),
        )

    result = refresh_kol_calls(bar_at, latest_prices={"FAST": 10.7})
    call = calls_for_ticker("FAST")[0]

    assert result["closed"] == 1
    assert call["status"] == "won"
    assert call["exit_at"] == bar_at.isoformat()
    assert call["benchmark_label"] == "up"


def test_abandoned_call_keeps_receiving_the_fixed_benchmark(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "kol-abandon.db")
    init_db()
    captured_at = datetime(2026, 8, 25, 15, tzinfo=UTC)
    _seed_prediction(
        "run-one",
        "snapshot-one",
        "DROP",
        captured_at,
        probability_up=0.7,
        expected_return_pct=2.0,
    )
    publish_calls_for_scan("run-one", "model-one")
    second_at = captured_at + timedelta(minutes=10)
    _seed_prediction(
        "run-two",
        "snapshot-two",
        "DROP",
        second_at,
        probability_up=0.2,
        expected_return_pct=-2.0,
        price=9.5,
    )

    result = publish_calls_for_scan("run-two", "model-one")
    abandoned = calls_for_ticker("DROP")[0]
    assert result["calls_abandoned"] == 1
    assert abandoned["status"] == "abandoned"
    assert abandoned["realized_return_pct"] == -5.0
    assert abandoned["benchmark_label"] is None

    settled_at = captured_at + timedelta(hours=1)
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_outcomes(
                snapshot_id,ticker,base_price,base_at,barrier_label,barrier_hit_at,
                return_60m_pct,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                "snapshot-one",
                "DROP",
                10.0,
                captured_at.isoformat(),
                "up",
                settled_at.isoformat(),
                8.2,
                settled_at.isoformat(),
            ),
        )
    refresh_kol_calls(settled_at, latest_prices={})
    settled = calls_for_ticker("DROP")[0]
    assert settled["status"] == "abandoned"
    assert settled["benchmark_label"] == "up"
    assert predictor_scorecards()[0]["hit_rate"] == 1.0
