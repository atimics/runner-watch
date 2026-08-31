from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.market_reports import (
    market_report_schedule,
    market_reports_overview,
    refresh_market_reports,
)


def _insert_scan_run(run_id: str, captured_at: str, candidate_rows: int) -> None:
    with connection() as database:
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
                candidate_rows,
                candidate_rows,
                candidate_rows,
                candidate_rows,
                "[]",
                "[]",
                captured_at,
                captured_at,
                captured_at,
            ),
        )


def _insert_snapshot(
    snapshot_id: str,
    run_id: str,
    ticker: str,
    score: float,
    rank: int,
    captured_at: str,
    *,
    session: str,
    price: float,
    change_pct: float,
    relative_volume: float = 3.0,
    trade_state: str = "WATCH",
) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at,
                scan_run_id,baseline_rank,trade_state
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                ticker,
                score,
                "BUILDING",
                session,
                price,
                change_pct,
                2.0,
                4.0,
                relative_volume,
                4.0,
                0.8,
                800_000,
                captured_at,
                '["Volume acceleration"]',
                "[]",
                captured_at,
                run_id,
                rank,
                trade_state,
            ),
        )


def test_pre_market_report_freezes_the_latest_pre_open_scan(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "pre-market.db")
    init_db()
    early_at = "2026-08-24T12:30:00+00:00"
    latest_at = "2026-08-24T12:55:00+00:00"
    _insert_scan_run("early-pre", early_at, 1)
    _insert_snapshot(
        "early-one",
        "early-pre",
        "OLD",
        20,
        1,
        early_at,
        session="pre",
        price=1.0,
        change_pct=2.0,
    )
    _insert_scan_run("latest-pre", latest_at, 2)
    _insert_snapshot(
        "latest-one",
        "latest-pre",
        "ONE",
        75,
        1,
        latest_at,
        session="pre",
        price=1.5,
        change_pct=12.0,
        relative_volume=6.0,
    )
    _insert_snapshot(
        "latest-two",
        "latest-pre",
        "RISK",
        50,
        2,
        latest_at,
        session="pre",
        price=0.8,
        change_pct=-3.0,
        trade_state="AVOID",
    )
    at = datetime(2026, 8, 24, 13, 5, tzinfo=UTC)

    first = refresh_market_reports(at)
    second = refresh_market_reports(at)
    overview = market_reports_overview(at)

    assert first["results"] == [
        {"report_type": "pre_market", "status": "created", "id": first["results"][0]["id"]}
    ]
    assert second["results"][0]["status"] == "current"
    report = overview["featured"]
    assert report["source_scan_run_id"] == "latest-pre"
    assert report["headline"] == "ONE leads the pre-market board"
    assert report["metrics"] == {
        "candidates": 2,
        "green": 1,
        "red": 1,
        "average_change_pct": 4.5,
        "median_relative_volume": 4.5,
        "high_risk": 1,
    }
    assert [row["ticker"] for row in report["leaders"]] == ["ONE", "RISK"]
    with connection() as database:
        saved = database.execute("SELECT COUNT(*) FROM market_session_reports").fetchone()[0]
    assert saved == 1


def test_post_market_report_compares_the_open_and_close_boards(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "post-market.db")
    init_db()
    pre_at = "2026-08-24T12:55:00+00:00"
    open_at = "2026-08-24T13:35:00+00:00"
    close_at = "2026-08-24T20:10:00+00:00"
    _insert_scan_run("pre", pre_at, 1)
    _insert_snapshot(
        "pre-one",
        "pre",
        "PRE",
        40,
        1,
        pre_at,
        session="pre",
        price=1.0,
        change_pct=3.0,
    )
    _insert_scan_run("open", open_at, 2)
    _insert_snapshot(
        "open-one",
        "open",
        "ONE",
        80,
        1,
        open_at,
        session="regular",
        price=2.0,
        change_pct=10.0,
    )
    _insert_snapshot(
        "open-two",
        "open",
        "TWO",
        60,
        2,
        open_at,
        session="regular",
        price=1.0,
        change_pct=5.0,
    )
    _insert_scan_run("close", close_at, 2)
    _insert_snapshot(
        "close-two",
        "close",
        "TWO",
        90,
        1,
        close_at,
        session="after",
        price=1.5,
        change_pct=25.0,
    )
    _insert_snapshot(
        "close-new",
        "close",
        "NEW",
        70,
        2,
        close_at,
        session="after",
        price=0.75,
        change_pct=8.0,
    )
    at = datetime(2026, 8, 24, 20, 15, tzinfo=UTC)

    result = refresh_market_reports(at)
    overview = market_reports_overview(at)
    report = overview["featured"]
    weekend_overview = market_reports_overview(datetime(2026, 8, 29, 16, 0, tzinfo=UTC))

    assert [item["status"] for item in result["results"]] == ["created", "created"]
    assert report["report_type"] == "post_market"
    assert report["headline"] == "TWO finishes on top"
    assert report["comparison_scan_run_id"] == "open"
    assert report["source_scan_run_id"] == "close"
    assert weekend_overview["featured"]["report_type"] == "post_market"
    assert {key: report["metrics"][key] for key in ("held", "joined", "dropped")} == {
        "held": 1,
        "joined": 1,
        "dropped": 1,
    }
    assert report["turns"] == [
        {
            "ticker": "NEW",
            "status": "joined",
            "open_rank": None,
            "close_rank": 2,
            "rank_change": None,
            "checkpoint_return_pct": None,
        },
        {
            "ticker": "TWO",
            "status": "held",
            "open_rank": 2,
            "close_rank": 1,
            "rank_change": 1,
            "checkpoint_return_pct": 50.0,
        },
        {
            "ticker": "ONE",
            "status": "dropped",
            "open_rank": 1,
            "close_rank": None,
            "rank_change": None,
            "checkpoint_return_pct": None,
        },
    ]


def test_market_report_schedule_waits_for_scans_and_skips_weekends(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "schedule.db")
    init_db()

    before_open = refresh_market_reports(datetime(2026, 8, 24, 12, 0, tzinfo=UTC))
    after_due = refresh_market_reports(datetime(2026, 8, 24, 13, 5, tzinfo=UTC))
    weekend = market_report_schedule(datetime(2026, 8, 29, 16, 0, tzinfo=UTC))

    assert before_open["results"] == []
    assert after_due["results"] == [
        {"report_type": "pre_market", "status": "awaiting_scan", "id": None}
    ]
    assert weekend["due"] == []
    assert weekend["is_market_day"] is False
    assert weekend["next_label"] == "Pre-market briefing"
    assert weekend["next_at"] == "2026-08-31T13:00:00+00:00"


def test_market_report_routes_and_template_are_public() -> None:
    root = Path(__file__).parents[1]
    source = (root / "src/runner_web/main.py").read_text()
    template = (root / "web/templates/market_reports.html").read_text()

    assert '@app.get("/reports"' in source
    assert '@app.get("/api/market-reports")' in source
    assert "Before the bell" in template
    assert "After the bell" in template
    assert "Frozen from scanner checkpoints" in template
