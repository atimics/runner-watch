from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    assert report["forecast_state"] == "queued"
    assert all(row["eod_forecast"] is None for row in report["leaders"])
    with connection() as database:
        saved = database.execute("SELECT COUNT(*) FROM market_session_reports").fetchone()[0]
        jobs = database.execute("SELECT COUNT(*) FROM market_report_forecast_jobs").fetchone()[0]
    assert saved == 1
    assert jobs == 1


def test_post_market_report_mirrors_the_watch_board_with_results(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "post-market.db")
    init_db()
    pre_at = "2026-08-24T12:55:00+00:00"
    open_at = "2026-08-24T13:35:00+00:00"
    close_at = "2026-08-24T20:10:00+00:00"
    _insert_scan_run("pre", pre_at, 2)
    _insert_snapshot(
        "pre-one",
        "pre",
        "ONE",
        80,
        1,
        pre_at,
        session="pre",
        price=1.0,
        change_pct=10.0,
    )
    _insert_snapshot(
        "pre-two",
        "pre",
        "TWO",
        60,
        2,
        pre_at,
        session="pre",
        price=2.0,
        change_pct=-4.0,
    )
    _insert_scan_run("open", open_at, 1)
    _insert_snapshot(
        "open-one",
        "open",
        "ONE",
        70,
        1,
        open_at,
        session="regular",
        price=1.1,
        change_pct=12.0,
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
        price=3.0,
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
    pre_report = overview["latest"]["pre_market"]
    weekend_overview = market_reports_overview(datetime(2026, 8, 29, 16, 0, tzinfo=UTC))

    assert [item["status"] for item in result["results"]] == ["created", "created"]
    assert report["report_type"] == "post_market"
    assert report["headline"] == "TWO led the watch board at +50.0%"
    assert report["comparison_scan_run_id"] == "pre"
    assert report["source_scan_run_id"] == "close"
    assert weekend_overview["featured"]["report_type"] == "post_market"
    assert [row["ticker"] for row in report["leaders"]] == ["ONE", "TWO"]
    assert report["metric_cards"] == pre_report["metric_cards"]
    assert report["record_cards"] == [
        {"label": "Flash record", "value": "0–0", "tone": "flat"},
        {"label": "Hit rate", "value": "—", "tone": "flat"},
        {"label": "Board W–L", "value": "1–0", "tone": "up"},
        {"label": "Avg move", "value": "+50.0%", "tone": "up"},
    ]
    assert pre_report["record_cards"] == []
    assert {key: report["metrics"][key] for key in ("held", "joined", "dropped")} == {
        "held": 1,
        "joined": 1,
        "dropped": 1,
    }
    assert {key: report["metrics"][key] for key in ("winners", "losers")} == {
        "winners": 1,
        "losers": 0,
    }
    dropped, held = report["leaders"]
    assert dropped["board_status"] == "dropped"
    assert dropped["close_price"] is None
    assert dropped["session_return_pct"] is None
    assert held["board_status"] == "held"
    assert held["close_rank"] == 1
    assert held["close_price"] == 3.0
    assert held["session_return_pct"] == 50.0
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


def test_post_market_report_keeps_the_pre_market_counts_on_a_wide_board(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "post-market-wide.db")
    init_db()
    pre_at = "2026-08-24T12:55:00+00:00"
    close_at = "2026-08-24T20:10:00+00:00"
    _insert_scan_run("pre", pre_at, 10)
    for index in range(10):
        _insert_snapshot(
            f"pre-{index}",
            "pre",
            f"T{index}",
            90 - index,
            index + 1,
            pre_at,
            session="pre",
            price=1.0,
            change_pct=3.0,
        )
    _insert_scan_run("close", close_at, 1)
    _insert_snapshot(
        "close-zero",
        "close",
        "T0",
        95,
        1,
        close_at,
        session="after",
        price=1.5,
        change_pct=40.0,
    )
    at = datetime(2026, 8, 24, 20, 15, tzinfo=UTC)

    refresh_market_reports(at)
    overview = market_reports_overview(at)
    pre_report = overview["latest"]["pre_market"]
    post_report = overview["latest"]["post_market"]

    assert pre_report["metrics"]["candidates"] == 10
    assert len(pre_report["leaders"]) == 8
    assert post_report["metrics"]["candidates"] == 10
    assert [row["ticker"] for row in post_report["leaders"]] == [
        row["ticker"] for row in pre_report["leaders"]
    ]
    assert post_report["metric_cards"] == pre_report["metric_cards"]
    assert post_report["metrics"]["dropped"] == 7


def test_post_market_report_falls_back_to_the_opening_scan(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "post-market-fallback.db")
    init_db()
    open_at = "2026-08-24T13:35:00+00:00"
    close_at = "2026-08-24T20:10:00+00:00"
    _insert_scan_run("open", open_at, 1)
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
    _insert_scan_run("close", close_at, 1)
    _insert_snapshot(
        "close-one",
        "close",
        "ONE",
        90,
        1,
        close_at,
        session="after",
        price=1.0,
        change_pct=-5.0,
    )
    at = datetime(2026, 8, 24, 20, 15, tzinfo=UTC)

    refresh_market_reports(at)
    report = market_reports_overview(at)["latest"]["post_market"]

    assert report["comparison_scan_run_id"] == "open"
    assert [row["ticker"] for row in report["leaders"]] == ["ONE"]
    assert report["leaders"][0]["session_return_pct"] == -50.0
    assert report["metrics"]["losers"] == 1


def test_market_report_schedule_waits_for_scans_and_skips_weekends(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "schedule.db")
    init_db()

    before_open = refresh_market_reports(datetime(2026, 8, 24, 7, 0, tzinfo=UTC))
    after_due = refresh_market_reports(datetime(2026, 8, 24, 8, 20, tzinfo=UTC))
    weekend = market_report_schedule(datetime(2026, 8, 29, 16, 0, tzinfo=UTC))

    assert before_open["results"] == []
    assert after_due["results"] == [
        {"report_type": "pre_market", "status": "awaiting_scan", "id": None}
    ]
    assert weekend["due"] == []
    assert weekend["is_market_day"] is False
    assert weekend["next_label"] == "Pre-market briefing"
    assert weekend["next_at"] == "2026-08-31T08:15:00+00:00"


def test_market_report_routes_and_template_are_public() -> None:
    root = Path(__file__).parents[1]
    source = (root / "src/runner_web/main.py").read_text()
    template = (root / "web/templates/market_reports.html").read_text()
    card = (root / "web/templates/_market_report_card.html").read_text()
    meta = (root / "web/templates/_market_report_meta.html").read_text()

    assert '@app.get("/reports"' in source
    assert '@app.get("/api/market-reports")' in source
    assert '@app.get("/reports/{report_day}/{slug}"' in source
    assert '@app.get("/reports/{report_day}/{slug}/card.png")' in source
    assert "Pre-market" in template
    assert "After the bell" in template
    assert "4:15 a.m. ET" in template
    assert "4:15 p.m. ET" in template
    assert "Desk commentary" in card
    assert "Frozen from scanner checkpoints" in card
    for tag in ("og:title", "og:description", "og:image", "twitter:card"):
        assert tag in meta


def _share_client(tmp_path: Path, monkeypatch: MonkeyPatch) -> Any:
    from starlette.testclient import TestClient

    from runner_web import main as web_main

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "share.db")
    init_db()
    pre_at = "2026-08-24T08:10:00+00:00"
    close_at = "2026-08-24T20:10:00+00:00"
    _insert_scan_run("pre", pre_at, 2)
    _insert_snapshot(
        "pre-one", "pre", "ONE", 80, 1, pre_at, session="pre", price=1.0, change_pct=10.0
    )
    _insert_snapshot(
        "pre-two", "pre", "TWO", 60, 2, pre_at, session="pre", price=2.0, change_pct=-4.0
    )
    _insert_scan_run("close", close_at, 1)
    _insert_snapshot(
        "close-two", "close", "TWO", 90, 1, close_at, session="after", price=3.0, change_pct=25.0
    )
    refresh_market_reports(datetime(2026, 8, 24, 20, 15, tzinfo=UTC))
    return TestClient(web_main.app, base_url=web_main.APP_ORIGIN)


def test_each_report_has_a_shareable_permalink_and_card(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    client = _share_client(tmp_path, monkeypatch)
    try:
        page = client.get("/reports/2026-08-24/post")
        card = client.get("/reports/2026-08-24/post/card.png")
        listing = client.get("/reports")
    finally:
        client.close()

    assert page.status_code == 200
    assert 'property="og:image"' in page.text
    assert 'name="twitter:card" content="summary_large_image"' in page.text
    assert "/reports/2026-08-24/post/card.png?v=" in page.text
    assert "Share this turn" in page.text
    assert card.status_code == 200
    assert card.headers["content-type"] == "image/png"
    assert "public" in card.headers["cache-control"]
    assert listing.status_code == 200
    assert 'property="og:image"' in listing.text
    assert 'href="/reports/2026-08-24/pre"' in listing.text


def test_share_metadata_names_the_top_pick_and_the_record(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "share-meta.db")
    init_db()
    pre_at = "2026-08-24T08:10:00+00:00"
    close_at = "2026-08-24T20:10:00+00:00"
    _insert_scan_run("pre", pre_at, 1)
    _insert_snapshot(
        "pre-one", "pre", "ONE", 80, 1, pre_at, session="pre", price=1.0, change_pct=10.0
    )
    _insert_scan_run("close", close_at, 1)
    _insert_snapshot(
        "close-one", "close", "ONE", 90, 1, close_at, session="after", price=1.5, change_pct=25.0
    )
    at = datetime(2026, 8, 24, 20, 15, tzinfo=UTC)
    refresh_market_reports(at)
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_report_forecasts(
                report_id,ticker,report_day,reference_price,reference_at,target_price,
                direction,reason,model,contract_version,forecast_at,status,close_price
            ) SELECT id,'ONE','2026-08-24',1.0,?,1.2,'up','Momentum','model','v1',?,'hit',1.5
            FROM market_session_reports WHERE report_type='pre_market'
            """,
            (pre_at, pre_at),
        )
    overview = market_reports_overview(at)
    pre_share = overview["latest"]["pre_market"]["share"]
    post_share = overview["latest"]["post_market"]["share"]

    assert pre_share["path"] == "/reports/2026-08-24/pre"
    assert pre_share["title"] == "$ONE · Flash targets $1.2 by the close"
    assert pre_share["top_pick"]["ticker"] == "ONE"
    assert post_share["path"] == "/reports/2026-08-24/post"
    assert post_share["title"] == "$ONE target hit · Flash 1–0 on the day"
    assert post_share["summary"].startswith("2026-08-24 · Flash 1–0 on targets, board 1–0")
    assert post_share["top_pick"]["status"] == "hit"
    assert post_share["card_path"] != pre_share["card_path"]
    assert len(post_share["summary"]) <= 200


def test_a_crafted_report_address_cannot_steer_the_sports_redirect(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from runner_web import main as web_main

    client = _share_client(tmp_path, monkeypatch)
    monkeypatch.setattr(web_main, "product_for_request", lambda _request: "sports")
    try:
        hijack = client.get(
            "/reports/2026-08-24%2F..%2F..%2Fevil.example.com/post", follow_redirects=False
        )
        bad_slug = client.get(
            "/reports/2026-08-24/https:%2F%2Fevil.example.com", follow_redirects=False
        )
        allowed = client.get("/reports/2026-08-24/post", follow_redirects=False)
    finally:
        client.close()

    assert hijack.status_code == 404
    assert bad_slug.status_code == 404
    assert allowed.status_code == 307
    assert allowed.headers["location"] == f"{web_main.RUNNERS_ORIGIN}/reports/2026-08-24/post"


def test_legacy_post_market_report_without_close_fields_still_renders(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """Reports frozen before the close board existed must still render."""

    from starlette.testclient import TestClient

    from runner_web import main as web_main

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "legacy-report.db")
    init_db()
    # A leader row written before the close board carried no close fields. The
    # report card formats leader.close_price, so a missing key used to raise.
    legacy_leaders = [
        {
            "ticker": "AAA",
            "company": "Alpha Co",
            "price": 1.23,
            "change_pct": 4.5,
            "rank": 1,
            "score": 80,
            "relative_volume": 3.2,
            "trade_state": "running",
            "session": "regular",
            "dollar_volume": 1_000_000,
            "stage": "running",
        }
    ]
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_session_reports(
                id,report_day,report_type,source_scan_run_id,comparison_scan_run_id,
                as_of,headline,summary,metrics_json,leaders_json,turns_json,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-post",
                "2026-09-10",
                "post_market",
                "run-close",
                "run-watch",
                "2026-09-10T20:15:00+00:00",
                "Legacy headline",
                "Legacy summary",
                '{"candidates":1}',
                json.dumps(legacy_leaders),
                "[]",
                "2026-09-10T20:15:00+00:00",
                "2026-09-10T20:15:00+00:00",
            ),
        )
    client = TestClient(web_main.app, base_url=web_main.APP_ORIGIN)
    try:
        listing = client.get("/reports")
        detail = client.get("/reports/2026-09-10/post")
    finally:
        client.close()

    assert listing.status_code == 200
    assert "Dropped off the board" in listing.text
    assert detail.status_code == 200
