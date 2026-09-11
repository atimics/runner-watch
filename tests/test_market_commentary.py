from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.market_commentary import (
    CONTRACT_VERSION,
    MAX_ATTEMPTS,
    VOICES_PER_REPORT,
    generate_report_commentary,
    queue_report_commentary,
    report_voices,
)
from runner_web.market_forecasts import queue_market_forecasts
from runner_web.market_reports import market_report

DAY = "2026-09-02"
PRE = datetime(2026, 9, 2, 8, 20, tzinfo=UTC)
CLOSE = datetime(2026, 9, 2, 20, 20, tzinfo=UTC)
LATE = datetime(2026, 9, 2, 21, 40, tzinfo=UTC)


@pytest.fixture(autouse=True)
def commentary_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "market-commentary.db")
    init_db()


def _leader(ticker: str, **overrides):
    return {
        "ticker": ticker,
        "rank": 1,
        "score": 70.0,
        "price": 1.0,
        "quote_time": PRE.isoformat(),
        "change_pct": 5.0,
        "relative_volume": 4.0,
        "trade_state": "WATCH",
        "signals": ["Volume acceleration"],
        "risks": [],
        **overrides,
    }


def _report(report_id: str, report_type: str, *, leaders=None, at=PRE, day=DAY, queue=True):
    leaders = [_leader("UP")] if leaders is None else leaders
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_session_reports(
                id,report_day,report_type,source_scan_run_id,as_of,headline,summary,
                metrics_json,leaders_json,turns_json,created_at,updated_at
            ) VALUES(?,?,?,'scan',?,'Watch board','Saved scan','{}',?,'[]',?,?)
            """,
            (
                report_id,
                day,
                report_type,
                at.isoformat(),
                json.dumps(leaders),
                at.isoformat(),
                at.isoformat(),
            ),
        )
        if queue:
            queue_report_commentary(database, report_id, day, report_type, at)
    return leaders


def _generate(request):
    return {
        "model": request["actor"]["model"],
        "request_id": "commentary-1",
        "analysis": {
            "headline": "The board is thin but awake.",
            "narrative": "Two names carry the volume into the open.",
            "points": ["Watch the opening range.", "Risk state stays intact."],
        },
        "comments": [
            {"voice_id": voice["id"], "comment": f"{voice['ability']} reads the tape."}
            for voice in request["voices"]
        ],
    }


def _comments(report_id: str = "pre"):
    with connection() as database:
        return [
            dict(row)
            for row in database.execute(
                "SELECT * FROM market_report_comments WHERE report_id=? ORDER BY position",
                (report_id,),
            ).fetchall()
        ]


def _job(report_id: str = "pre"):
    with connection() as database:
        row = database.execute(
            "SELECT * FROM market_report_commentary_jobs WHERE report_id=?",
            (report_id,),
        ).fetchone()
    return dict(row) if row else None


def test_report_voices_are_stable_and_distinct():
    first = report_voices("pre")
    again = report_voices("pre")
    other = report_voices("post")

    assert len(first) == VOICES_PER_REPORT
    assert [voice["id"] for voice in first] == [voice["id"] for voice in again]
    assert len({voice["id"] for voice in first}) == VOICES_PER_REPORT
    assert {voice["id"] for voice in first} != {voice["id"] for voice in other}
    assert all(voice["avatar"]["name"] == voice["name"] for voice in first)


def test_pre_market_commentary_is_generated_and_attached():
    _report("pre", "pre_market")

    assert generate_report_commentary(_generate, PRE) == {
        "completed": 1,
        "failed": 0,
        "waiting": 0,
    }

    report = market_report(DAY, "pre_market")
    assert report["commentary_state"] == "complete"
    assert report["analysis"]["headline"] == "The board is thin but awake."
    assert report["analysis"]["contract_version"] == CONTRACT_VERSION
    assert len(report["desk_comments"]) == VOICES_PER_REPORT
    assert [comment["id"] for comment in report["desk_comments"]] == [
        voice["id"] for voice in report_voices("pre")
    ]
    assert all(comment["avatar"]["ability"] for comment in report["desk_comments"])
    assert generate_report_commentary(_generate, PRE)["completed"] == 0


def test_post_market_commentary_waits_for_pending_targets():
    _report("pre", "pre_market", queue=False)
    _report("post", "post_market", at=CLOSE)
    with connection() as database:
        queue_market_forecasts(database, "pre", DAY, [_leader("UP")], PRE)
        database.execute(
            """
            INSERT INTO market_report_forecasts(
                report_id,ticker,report_day,reference_price,reference_at,target_price,
                direction,reason,model,contract_version,forecast_at,status
            ) VALUES('pre','UP',?,1.0,?,1.2,'up','Momentum','model','v1',?,'pending')
            """,
            (DAY, PRE.isoformat(), PRE.isoformat()),
        )

    assert generate_report_commentary(_generate, CLOSE) == {
        "completed": 0,
        "failed": 0,
        "waiting": 1,
    }
    assert _job("post")["status"] == "queued"

    with connection() as database:
        database.execute(
            "UPDATE market_report_forecasts SET status='hit',close_price=1.3 WHERE ticker='UP'"
        )

    assert generate_report_commentary(_generate, CLOSE)["completed"] == 1
    request = json.loads(_job("post")["request_json"])
    assert request["report_type"] == "post_market"
    assert request["report"]["target_record"]["label"] == "1–0"
    assert request["report"]["leaders"][0]["eod_target"]["status"] == "hit"


def test_post_market_commentary_stops_waiting_after_the_settlement_window():
    _report("pre", "pre_market", queue=False)
    _report("post", "post_market", at=CLOSE)
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_report_forecasts(
                report_id,ticker,report_day,reference_price,reference_at,target_price,
                direction,reason,model,contract_version,forecast_at,status
            ) VALUES('pre','UP',?,1.0,?,1.2,'up','Momentum','model','v1',?,'pending')
            """,
            (DAY, PRE.isoformat(), PRE.isoformat()),
        )

    assert generate_report_commentary(_generate, CLOSE)["waiting"] == 1
    assert generate_report_commentary(_generate, LATE)["completed"] == 1


def test_a_ready_job_runs_ahead_of_a_waiting_one():
    _report("post", "post_market", at=CLOSE)
    _report("next-pre", "pre_market", at=LATE, day="2026-09-03")
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_report_forecasts(
                report_id,ticker,report_day,reference_price,reference_at,target_price,
                direction,reason,model,contract_version,forecast_at,status
            ) VALUES('post','UP',?,1.0,?,1.2,'up','Momentum','model','v1',?,'pending')
            """,
            (DAY, PRE.isoformat(), PRE.isoformat()),
        )

    result = generate_report_commentary(_generate, CLOSE)

    assert result == {"completed": 1, "failed": 0, "waiting": 1}
    assert _job("next-pre")["status"] == "complete"
    assert _job("post")["status"] == "queued"


@pytest.mark.parametrize(
    "damage",
    [
        {"model": "other/model"},
        {"analysis": {"headline": "", "narrative": "Body", "points": []}},
        {"analysis": {"headline": "Head", "narrative": "  ", "points": []}},
        {"comments": []},
        {"comments": [{"voice_id": "missing", "comment": "Hello"}]},
    ],
)
def test_invalid_commentary_is_retried_then_failed(damage):
    _report("pre", "pre_market")

    def generate(request):
        return {**_generate(request), **damage}

    for _attempt in range(MAX_ATTEMPTS):
        assert generate_report_commentary(generate, PRE)["failed"] == 1
    assert _job()["status"] == "failed"
    assert generate_report_commentary(generate, PRE)["failed"] == 0
    assert _comments() == []
    assert market_report(DAY, "pre_market")["analysis"] is None


def test_duplicate_voices_and_long_bodies_are_rejected_or_trimmed():
    _report("pre", "pre_market")

    def duplicated(request):
        result = _generate(request)
        result["comments"][1]["voice_id"] = result["comments"][0]["voice_id"]
        return result

    assert generate_report_commentary(duplicated, PRE)["failed"] == 1

    def verbose(request):
        result = _generate(request)
        result["analysis"]["narrative"] = "word " * 400
        result["comments"][0]["comment"] = "long " * 200
        return result

    assert generate_report_commentary(verbose, PRE)["completed"] == 1
    report = market_report(DAY, "pre_market")
    assert len(report["analysis"]["narrative"]) <= 900
    assert len(report["desk_comments"][0]["body"]) <= 240


def test_commentary_is_skipped_without_a_model():
    _report("pre", "pre_market")

    assert generate_report_commentary(None, PRE) == {"completed": 0, "failed": 0, "waiting": 0}
    assert _job()["status"] == "queued"
    assert market_report(DAY, "pre_market")["commentary_state"] == "queued"


def test_reports_frozen_before_the_desk_have_no_commentary():
    _report("pre", "pre_market", queue=False)

    report = market_report(DAY, "pre_market")

    assert report["commentary_state"] == "legacy"
    assert report["desk_comments"] == []
    assert report["analysis"] is None


def test_report_page_shows_the_analysis_and_desk_comments(monkeypatch):
    from starlette.requests import Request

    from runner_web import main as web_main
    from runner_web.market_reports import market_reports_overview

    _report("pre", "pre_market")
    generate_report_commentary(_generate, PRE)
    overview = market_reports_overview(PRE)
    monkeypatch.setattr(web_main, "market_reports_overview", lambda **_kwargs: overview)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/reports",
            "query_string": b"",
            "headers": [(b"host", b"runners.rati.chat")],
            "scheme": "https",
            "server": ("runners.rati.chat", 443),
            "client": ("127.0.0.1", 1234),
        }
    )
    request.state.csp_nonce = "preview"

    html = web_main.market_reports_page(request, None).body.decode()

    assert "Flash reads the session ahead" in html
    assert "The board is thin but awake." in html
    assert "Watch the opening range." in html
    assert "Desk commentary" in html
    for voice in report_voices("pre"):
        assert voice["name"] in html
    assert Path("web/templates/market_reports.html").exists()
