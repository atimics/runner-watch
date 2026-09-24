from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from runner_web import attention_trial as trial
from runner_web import db
from runner_web.attention_model import artifact, probability
from runner_web.db import connection, init_db

AT = datetime(2026, 9, 23, 15, 1, tzinfo=UTC)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "attention.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setenv("ATTENTION_SHADOW_ENABLED", "1")
    monkeypatch.setattr(trial, "utcnow", lambda: AT)
    init_db()


def snapshots(n=12):
    return [
        {
            "id": f"snapshot-{i}",
            "scan_run_id": "scan",
            "ticker": f"T{i:02d}",
            "captured_at": AT.isoformat(),
            "quote_time": AT.isoformat(),
            "session": "REGULAR",
            "price": 10,
            "baseline_rank": i + 1,
            "change_pct": i,
            "momentum_5m_pct": i / 2,
            "momentum_15m_pct": i / 3,
            "momentum_previous_5m_pct": -1,
            "intraday_volatility_pct": i + 0.1,
            "relative_volume": 1 + i,
            "recent_relative_volume": 2,
            "dollar_volume": 1000,
            "recent_dollar_volume": 10,
            "average_dollar_volume": 5000,
        }
        for i in range(n)
    ]


def board(rows):
    return [
        {
            "attention_score": 10 + i,
            "score_components": {
                "market": i,
                "sec_event": 10,
                "news": 0,
                "social_search": 0,
                "cluster": 0,
                "community": 0,
            },
            "eligibility": {"status": "eligible"},
            "attention_urgent": i == 0,
            "attention_urgency_reason": "Active trading halt" if i == 0 else None,
        }
        for i, row in enumerate(rows)
    ]


def seed_run(rows=None, *, saved_at=AT):
    rows = rows or snapshots()
    with connection() as conn:
        conn.execute(
            """INSERT INTO attention_trial_runs(
                id,scan_run_id,model_id,model_sha256,policy,contract_json,evidence_as_of,
                day,status,expected_rows,build_sha
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "scan",
                "scan",
                artifact()["id"],
                artifact()["sha256"],
                trial.attention.POLICY_VERSION,
                trial.encode(trial.CONTRACT),
                saved_at.isoformat(),
                saved_at.date().isoformat(),
                "recording",
                len(rows),
                "test",
            ),
        )
    trial.persist_predictions("scan", rows, board(rows), saved_at)


def bar_rows(ticker="T00", *, at=AT + timedelta(hours=2), high=10.5):
    start = trial._window(AT)[0]
    return [
        (
            "yahoo",
            ticker,
            "5m",
            (start + timedelta(minutes=5 * i)).isoformat(),
            10.0,
            high,
            9.9,
            10.0,
            100,
            at.isoformat(),
            at.isoformat(),
        )
        for i in range(12)
    ]


def test_packaged_trees_match_native_model_with_missing_values():
    import lightgbm as lgb

    source = Path(__file__).resolve().parents[1] / (
        "docs/research/attention-study-2026-09-24/attention-candidate.txt"
    )
    model = lgb.Booster(model_file=str(source))
    matrix = np.random.default_rng(10).normal(3, 10, size=(200, 20))
    matrix[::3, ::2] = np.nan
    matrix[1] = np.nan
    matrix[2] = 0
    assert [probability(row.tolist()) for row in matrix] == pytest.approx(
        model.predict(matrix),
        abs=1e-12,
    )
    assert probability([None] * 20) == pytest.approx(model.predict(matrix)[1])


@pytest.mark.parametrize(
    "stamp,valid",
    [
        ("2026-09-23T13:30:00+00:00", True),
        ("2026-09-23T19:00:00+00:00", True),
        ("2026-09-23T19:00:01+00:00", False),
        ("2026-11-27T17:00:00+00:00", True),  # Early close at 18:00 UTC.
        ("2026-11-27T17:00:01+00:00", False),
        ("2026-11-26T15:00:00+00:00", False),
        ("2026-09-26T15:00:00+00:00", False),
        ("2026-09-23T13:29:00+00:00", False),
    ],
)
def test_session_windows_use_the_exchange_calendar(stamp, valid):
    assert bool(trial._window(datetime.fromisoformat(stamp))) is valid


def test_freezes_full_board_inputs_fallback_and_urgent_order(database, monkeypatch):
    rows = snapshots()
    rows[3]["quote_time"] = (AT - timedelta(hours=1)).isoformat()
    rows[4]["quote_time"] = (AT + timedelta(seconds=1)).isoformat()
    rows[5]["price"] = 0
    saved = AT + timedelta(minutes=5)
    monkeypatch.setattr(trial, "utcnow", lambda: saved)
    seed_run(rows)
    with connection() as conn:
        run = dict(conn.execute("SELECT * FROM attention_trial_runs").fetchone())
        predictions = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM attention_trial_predictions ORDER BY ticker"
            ).fetchall()
        ]
    assert run["saved_at"] == saved.isoformat()
    assert run["entry_at"] == "2026-09-23T15:10:00+00:00"
    assert run["expected_rows"] == len(predictions) == 12
    assert predictions[0]["baseline_rank"] == predictions[0]["candidate_rank"] == 1
    assert len(json.loads(predictions[1]["vector_json"])) == 20
    for i in (3, 4, 5):
        assert predictions[i]["probability"] is None
        assert predictions[i]["baseline_score"] == predictions[i]["candidate_score"]
    assert json.loads(predictions[3]["inputs_json"])["quote_time"] == rows[3]["quote_time"]
    assert json.loads(predictions[0]["evidence_json"])["components"]["sec_event"] == 10


def test_all_fallback_and_close_crossing_remain_durable(database, monkeypatch):
    rows = snapshots()
    for row in rows:
        row["quote_time"] = None
    monkeypatch.setattr(trial, "utcnow", lambda: AT.replace(hour=20))
    seed_run(rows)
    with connection() as conn:
        assert conn.execute("SELECT status FROM attention_trial_runs").fetchone()[0] == (
            "outside_window"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM attention_trial_predictions "
                "WHERE outcome_status='outside_window'"
            ).fetchone()[0]
            == 12
        )


def test_completed_unchanged_bar_is_archived_and_revisions_keep_first_outcome(database):
    seed_run()
    start, end = trial._window(AT)
    with connection() as conn:
        trial.archive_bars(conn, bar_rows(at=start))  # All bars still forming.
        assert conn.execute("SELECT COUNT(*) FROM attention_trial_bars").fetchone()[0] == 0
        trial.archive_bars(conn, bar_rows(at=end))
        trial.archive_bars(conn, bar_rows(at=end + timedelta(minutes=1)))
        trial.archive_bars(conn, bar_rows(at=end + timedelta(minutes=2), high=10.1))
        assert conn.execute("SELECT COUNT(*) FROM attention_trial_bars").fetchone()[0] == 24
    trial.refresh_outcomes(
        fetch=lambda _: SimpleNamespace(failed=[]), at=end + timedelta(minutes=6)
    )
    with connection() as conn:
        row = dict(
            conn.execute("SELECT * FROM attention_trial_predictions WHERE ticker='T00'").fetchone()
        )
    assert row["target"] == 1
    assert row["outcome_status"] == "resolved"
    receipt = json.loads(row["outcome_json"])
    assert len(receipt["bar_hashes"]) == 12
    assert receipt["last_revision_at"] == end.isoformat()


def test_missing_bar_stays_pending_then_unknown_and_late_data_is_excluded(database):
    seed_run(snapshots(1))
    end = trial._window(AT)[1]
    with connection() as conn:
        trial.archive_bars(conn, bar_rows(at=end)[:-1])
    trial.refresh_outcomes(
        fetch=lambda _: SimpleNamespace(failed=["T00"]), at=end + timedelta(minutes=6)
    )
    with connection() as conn:
        row = dict(conn.execute("SELECT * FROM attention_trial_predictions").fetchone())
        assert row["outcome_status"] == "pending"
        assert row["attempts"] == 1
        assert row["last_error"] == "provider_missing_tickers"
        trial.archive_bars(conn, bar_rows(at=end + timedelta(hours=7)))
    trial.refresh_outcomes(
        fetch=lambda _: pytest.fail("Deadline has passed"), at=end + timedelta(hours=7)
    )
    with connection() as conn:
        row = dict(conn.execute("SELECT * FROM attention_trial_predictions").fetchone())
    assert row["outcome_status"] == "unknown" and row["target"] is None
    assert json.loads(row["outcome_json"])["status"] == "missing_bar"


def test_fetch_queue_is_bounded_and_rotates(database):
    seed_run(snapshots(70))
    tickers = []

    def fetch(symbols):
        tickers.extend(symbols)
        raise RuntimeError("simulated source failure")

    at = AT + timedelta(hours=2)
    trial.refresh_outcomes(fetch=fetch, at=at)
    assert len(tickers) == 30
    trial.refresh_outcomes(fetch=fetch, at=at + timedelta(minutes=2))
    assert len(tickers) == len(set(tickers)) == 60


def test_ingestion_collects_unchanged_final_observation(database, monkeypatch):
    from runner_watch.ingestion import SourceFetch
    from runner_web.ingestion import _market_projection

    seed_run(snapshots(1))
    start, end = trial._window(AT)
    frame = pd.DataFrame(
        {"Open": [10], "High": [10.5], "Low": [9.9], "Close": [10], "Volume": [100]},
        index=pd.DatetimeIndex([start]),
    )
    fetch = SourceFetch.success(
        source="yahoo",
        feed="market_bars",
        locator="test",
        started_at=start.isoformat(),
        payload={"T00": frame},
        metadata={"interval": "5m"},
    )
    with connection() as conn:
        # The projection's item receipts require a parent ingestion run.
        conn.execute("PRAGMA foreign_keys=OFF")
        _market_projection(conn, "test-one", fetch, start.isoformat())
        _market_projection(conn, "test-two", fetch, end.isoformat())
        assert conn.execute("SELECT COUNT(*) FROM attention_trial_bars").fetchone()[0] == 1
        assert conn.execute("SELECT last_collected_at FROM market_bars").fetchone()[0] == (
            start.isoformat()
        )


def test_report_keeps_unknown_slots_and_requires_calibration(database, monkeypatch):
    seed_run()
    end = trial._window(AT)[1]
    with connection() as conn:
        trial.archive_bars(conn, bar_rows(at=end))
    trial.refresh_outcomes(at=end + timedelta(hours=7))
    monkeypatch.setattr(trial, "utcnow", lambda: AT + timedelta(days=1))
    report = trial.release_report()
    assert report["completed_sessions"] == 1
    assert report["calibration_sessions_remaining"] == 9
    assert report["full_board"]["candidate"]["selected"] == 10
    assert report["full_board"]["candidate"]["coverage"] == 0.1
    assert report["full_board"]["coverage_gate"] is False
    assert report["promotion_ready"] is False
    assert report["outcomes"] == {"resolved": 1, "unknown": 11}
    trial.encode(report)


def test_capture_is_idempotent_and_uses_exact_scan(database, monkeypatch):
    from runner_web import main

    rows = snapshots()
    seen = []

    def inputs(**kwargs):
        seen.append(kwargs)
        return {"market_rows": rows, "latest_run": {"candidate_rows": len(rows)}}

    monkeypatch.setattr(main, "_pulse_scoring_inputs", inputs)
    monkeypatch.setattr(main, "_pulse_snapshot_score", lambda row, _: board([row])[0])
    assert trial.capture_scan("scan") == "recorded"
    assert trial.capture_scan("scan") == "already_recorded"
    assert seen == [{"at": AT, "scan_run_id": "scan"}]
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attention_trial_predictions").fetchone()[0] == 12


def test_capture_error_is_a_durable_receipt(database, monkeypatch):
    from runner_web import main

    monkeypatch.setattr(main, "_pulse_scoring_inputs", lambda **_: {"market_rows": []})
    assert trial.capture_scan("scan") == "capture_error"
    assert trial.release_report()["runs"] == {"capture_error": 1}


def test_required_worker_tracks_the_flag(monkeypatch):
    from runner_web.operations import required_worker_names

    monkeypatch.setenv("ATTENTION_SHADOW_ENABLED", "1")
    assert "attention-shadow" in required_worker_names(sports_ingestion_enabled=False)
    monkeypatch.setenv("ATTENTION_SHADOW_ENABLED", "0")
    assert "attention-shadow" not in required_worker_names(sports_ingestion_enabled=False)


def test_capture_matches_real_board_and_survives_scan_cleanup(database):
    from runner_web import main

    rows = snapshots()
    with connection() as conn:
        for scan_id, at in (("scan", AT - timedelta(seconds=10)), ("newer", AT)):
            conn.execute(
                """INSERT INTO scan_runs(id,mode,label,feature_schema_version,requested_symbols,
                liquid_symbols,scanned_symbols,candidate_rows,failed_symbols_json,warnings_json,
                started_at,finished_at,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    scan_id,
                    "test",
                    "test",
                    "1",
                    12,
                    12,
                    12,
                    12,
                    "[]",
                    "[]",
                    at.isoformat(),
                    at.isoformat(),
                    at.isoformat(),
                ),
            )
        for row in rows:
            snapshot = {
                **row,
                "score": 0,
                "stage": "watch",
                "breakout_pct": 0,
                "signals_json": "[]",
                "risks_json": "[]",
                "trade_state": "WATCH",
                "rug_score": 0,
            }
            conn.execute(
                f"INSERT INTO scan_snapshots({','.join(snapshot)}) "
                f"VALUES({','.join('?' for _ in snapshot)})",
                tuple(snapshot.values()),
            )
    # A newer run exists, while this trial must use the requested scan's complete board.
    assert trial.capture_scan("scan") == "recorded"
    inputs = main._pulse_scoring_inputs(at=AT, scan_run_id="scan")
    expected = sorted(
        [{**row, **main._pulse_snapshot_score(row, inputs)} for row in inputs["market_rows"]],
        key=trial.attention.attention_order,
    )
    with connection() as conn:
        actual = conn.execute(
            "SELECT ticker,baseline_score FROM attention_trial_predictions ORDER BY baseline_rank"
        ).fetchall()
        assert [(r["ticker"], r["baseline_score"]) for r in actual] == [
            (r["ticker"], r["attention_score"]) for r in expected
        ]
        conn.execute("DELETE FROM scan_snapshots")
        conn.execute("DELETE FROM scan_runs")
        assert conn.execute("SELECT COUNT(*) FROM attention_trial_predictions").fetchone()[0] == 12


def test_pending_session_waits_for_close_and_outcome_allowance(database, monkeypatch):
    seed_run(snapshots(1))
    end = trial._window(AT)[1]
    with connection() as conn:
        trial.archive_bars(conn, bar_rows(at=end))
    trial.refresh_outcomes(at=end + timedelta(minutes=10))
    monkeypatch.setattr(trial, "utcnow", lambda: end + timedelta(minutes=10))
    assert trial.release_report()["completed_sessions"] == 0
    monkeypatch.setattr(trial, "utcnow", lambda: AT + timedelta(days=1))
    assert trial.release_report()["completed_sessions"] == 1
