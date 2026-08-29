from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.ranker import FEATURE_NAMES, ranker_status
from runner_web.ranker_history import backfill_historical_training


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


def test_historical_replay_writes_only_compact_point_in_time_rows(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "history.db")
    init_db()
    replay_at = datetime(2026, 8, 4, 14, 5, tzinfo=UTC)
    _seed_market_bars(replay_at)

    result = backfill_historical_training(
        days=2,
        cadence_minutes=30,
        target_groups=1,
        start_at=replay_at,
        end_at=replay_at,
        near_live_minutes=0,
    )

    assert result["groups_written"] == 1
    assert result["rows_written"] == 3
    with connection() as database:
        rows = database.execute(
            "SELECT * FROM ranker_training_examples ORDER BY ticker"
        ).fetchall()
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

    aaa = next(row for row in rows if row["ticker"] == "AAA")
    vector = json.loads(aaa["feature_vector_json"])
    log_price = vector[FEATURE_NAMES.index("log_price")]
    assert log_price == round(math.log1p(1.0) * 1_000)
    assert ranker_status()["complete_groups"] == 1
    assert ranker_status()["training_origins"]["historical_replay"]["groups"] == 1


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
        assert database.execute(
            "SELECT COUNT(*) FROM ranker_training_examples"
        ).fetchone()[0] == 0

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
