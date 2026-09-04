from __future__ import annotations

import json
import math
import tomllib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from pytest import MonkeyPatch

from runner_web import database as database_module
from runner_web import db, ranker, ranker_history
from runner_web.db import connection, init_db
from runner_web.ranker import FEATURE_NAMES, ranker_status
from runner_web.ranker_history import (
    DEFAULT_CADENCE_MINUTES,
    ArchivedMarketData,
    _bar_tuples,
    _outcome_window,
    _replay_times,
    backfill_historical_training,
)


def test_default_replay_cadence_can_fill_the_training_window() -> None:
    assert DEFAULT_CADENCE_MINUTES == 15
    fly_config = tomllib.loads((Path(__file__).parents[1] / "fly.toml").read_text())
    assert int(fly_config["env"]["RANKER_HISTORICAL_BACKFILL_CADENCE_MINUTES"]) == 15


def test_replay_times_only_cover_regular_market_hours() -> None:
    frames = {"AAA": pd.DataFrame(index=pd.to_datetime(["2026-08-19T13:30:00Z"], utc=True))}

    points = _replay_times(
        frames,
        datetime(2026, 8, 19, tzinfo=UTC),
        datetime(2026, 8, 20, tzinfo=UTC),
        15,
    )
    local_times = [point.astimezone(ranker_history.EASTERN).time() for point in points]

    assert len(points) == 26
    assert local_times[0].isoformat() == "09:35:00"
    assert local_times[-1].isoformat() == "15:50:00"


def _seed_market_bars(replay_at: datetime) -> None:
    tickers = {"AAA": 1.0, "BBB": 2.0, "CCC": 3.0}
    rows: list[tuple[object, ...]] = []
    collected = (replay_at + timedelta(days=1)).isoformat()
    for ticker, price in tickers.items():
        for offset in range(30, 0, -1):
            stamp = replay_at - timedelta(days=offset)
            if stamp.weekday() >= 5:
                continue
            rows.append(
                (
                    "yahoo",
                    ticker,
                    "1d",
                    stamp.replace(hour=0, minute=0).isoformat(),
                    price,
                    price * 1.01,
                    price * 0.99,
                    price,
                    1_000_000,
                    collected,
                    collected,
                )
            )
        for session_offset in (1, 0):
            session = (replay_at - timedelta(days=session_offset)).astimezone(UTC)
            start = session.replace(hour=8, minute=0, second=0, microsecond=0)
            finish = (
                session.replace(hour=20, minute=0, second=0, microsecond=0)
                if session_offset
                else replay_at + timedelta(hours=1)
            )
            stamp = start
            while stamp <= finish:
                close = price
                high = price * 1.002
                low = price * 0.998
                if session_offset == 0 and stamp > replay_at:
                    if ticker == "AAA":
                        high = price * 1.09
                        close = price * 1.08
                    elif ticker == "BBB":
                        low = price * 0.95
                        close = price * 0.96
                rows.append(
                    (
                        "yahoo",
                        ticker,
                        "5m",
                        stamp.isoformat(),
                        price,
                        high,
                        low,
                        close,
                        100_000,
                        collected,
                        collected,
                    )
                )
                stamp += timedelta(minutes=5)
    with connection() as database:
        database.executemany(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,open,high,low,close,volume,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )


def test_archived_market_data_slices_only_requested_frames() -> None:
    index = pd.to_datetime(
        [
            "2026-08-04T13:55:00Z",
            "2026-08-04T14:00:00Z",
            "2026-08-04T14:05:00Z",
        ],
        utc=True,
    )
    intraday = {
        ticker: pd.DataFrame({"Close": [price, price + 0.1, price + 0.2]}, index=index)
        for ticker, price in {"AAA": 1.0, "BBB": 2.0}.items()
    }
    provider = ArchivedMarketData(
        {},
        intraday,
        intraday_start=datetime(2026, 8, 4, 14, 0, tzinfo=UTC),
        intraday_cutoff=datetime(2026, 8, 4, 14, 5, tzinfo=UTC),
    )

    result = provider.intraday(["AAA"])

    assert list(result.frames) == ["AAA"]
    assert len(result.frames["AAA"]) == 2
    assert len(intraday["AAA"]) == 3


def test_outcome_window_reuses_bars_and_keeps_the_full_label_horizon() -> None:
    replay_at = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)
    index = pd.date_range(
        replay_at - timedelta(minutes=5),
        replay_at + timedelta(minutes=75),
        freq="5min",
    )
    frame = pd.DataFrame(
        {
            "High": [1.01] * len(index),
            "Low": [0.99] * len(index),
            "Close": [1.0] * len(index),
        },
        index=index,
    )
    bars = _bar_tuples(frame)

    window = _outcome_window(bars, [bar[0] for bar in bars], replay_at)

    assert window[0][0] == replay_at + timedelta(minutes=5)
    assert window[-1][0] == replay_at + timedelta(minutes=70)
    assert len(window) == 14


def test_historical_replay_writes_only_compact_point_in_time_rows(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "history.db")
    init_db()
    replay_at = datetime(2026, 8, 4, 14, 5, tzinfo=UTC)
    _seed_market_bars(replay_at)
    progress: list[dict[str, object]] = []
    real_connection = ranker_history.connection
    write_attempts = 0

    @contextmanager
    def flaky_write_connection():
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            raise RuntimeError("temporary database outage")
        with real_connection() as database:
            yield database

    def record_progress(event: dict[str, object]) -> None:
        progress.append(event)
        if event.get("point") == 1:
            monkeypatch.setattr(ranker_history, "connection", flaky_write_connection)

    monkeypatch.setattr(database_module, "sleep", lambda _delay: None)

    result = backfill_historical_training(
        days=2,
        cadence_minutes=30,
        target_groups=1,
        start_at=replay_at,
        end_at=replay_at,
        near_live_minutes=0,
        progress=record_progress,
    )

    assert result["groups_written"] == 1
    assert result["rows_written"] == 3
    assert write_attempts == 2
    assert any(
        event.get("stage") == "loading_5m_bars" and event.get("tickers_loaded") == 3
        for event in progress
    )
    with connection() as database:
        rows = database.execute("SELECT * FROM ranker_training_examples ORDER BY ticker").fetchall()
        public_counts = {
            table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("scan_runs", "scan_snapshots", "pulse_entries")
        }
    assert public_counts == {"scan_runs": 0, "scan_snapshots": 0, "pulse_entries": 0}
    assert {row["training_origin"] for row in rows} == {"historical_replay"}
    assert {row["barrier_label"] for row in rows} == {"up", "down", "timeout"}
    provenance = json.loads(rows[0]["provenance_json"])
    assert provenance["point_in_time_features"] is True
    assert provenance["market_bar_source"] == "yahoo"
    assert provenance["bar_completion_lag_minutes"] == 5
    assert provenance["cohort_max_symbols"] == 36

    aaa = next(row for row in rows if row["ticker"] == "AAA")
    vector = json.loads(aaa["feature_vector_json"])
    log_price = vector[FEATURE_NAMES.index("log_price")]
    assert log_price == round(math.log1p(1.0) * 1_000)
    assert ranker_status()["complete_groups"] == 1
    assert ranker_status()["training_origins"]["historical_replay"]["groups"] == 1


def test_trainer_state_retries_a_temporary_database_outage(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "trainer-state.db")
    init_db()
    real_connection = ranker.connection
    attempts = 0

    @contextmanager
    def flaky_connection():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary database outage")
        with real_connection() as database:
            yield database

    monkeypatch.setattr(ranker, "connection", flaky_connection)
    monkeypatch.setattr(database_module, "sleep", lambda _delay: None)

    ranker._trainer_state("ranker_test_state", {"status": "ok"})

    assert attempts == 2
    with connection() as database:
        state = database.execute(
            "SELECT value FROM worker_state WHERE key=?", ("ranker_test_state",)
        ).fetchone()
    assert json.loads(state["value"]) == {"status": "ok"}


def test_historical_replay_is_idempotent_and_respects_dry_run(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "history-repeat.db")
    init_db()
    replay_at = datetime(2026, 8, 4, 14, 5, tzinfo=UTC)
    _seed_market_bars(replay_at)

    dry_run = backfill_historical_training(
        target_groups=1,
        start_at=replay_at,
        end_at=replay_at,
        near_live_minutes=0,
        dry_run=True,
    )
    assert dry_run["groups_written"] == 1
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM ranker_training_examples").fetchone()[0] == 0

    first = backfill_historical_training(
        target_groups=1,
        start_at=replay_at,
        end_at=replay_at,
        near_live_minutes=0,
    )
    second = backfill_historical_training(
        target_groups=1,
        start_at=replay_at,
        end_at=replay_at,
        near_live_minutes=0,
    )
    assert first["groups_written"] == 1
    assert second["groups_written"] == 0
    assert second["reason"] == "target_already_met"
