from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from runner_watch.source_catalog import SourcePolicy
from runner_web import db, market_forecasts
from runner_web.ai_kol import FLASH
from runner_web.db import connection, init_db
from runner_web.ingestion import register_source
from runner_web.market_forecasts import (
    CONTRACT_VERSION,
    generate_market_forecasts,
    queue_market_forecasts,
    settle_market_forecasts,
)
from runner_web.market_reports import market_report, market_reports_overview

DAY = "2026-09-02"
PRE = datetime(2026, 9, 2, 13, 5, tzinfo=UTC)
CLOSE = datetime(2026, 9, 2, 20, 20, tzinfo=UTC)


@pytest.fixture(autouse=True)
def forecast_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "market-forecasts.db")
    init_db()


def _report(*, leaders=None, at=PRE, report_id="report", queue=True):
    if leaders is None:
        leaders = [_leader("UP"), _leader("DOWN")]
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_session_reports(
                id,report_day,report_type,source_scan_run_id,as_of,headline,summary,
                leaders_json,created_at,updated_at
            ) VALUES(?,?,'pre_market','scan',?,'Watch board','Saved scan',?,?,?)
            """,
            (report_id, DAY, at.isoformat(), json.dumps(leaders), at.isoformat(), at.isoformat()),
        )
        if queue:
            queue_market_forecasts(database, report_id, DAY, leaders, at)


def _leader(ticker: str, **overrides):
    return {
        "ticker": ticker,
        "price": 1.0,
        "quote_time": "2026-09-02T13:00:00+00:00",
        "trade_state": "WATCH",
        "rank": 1,
        "score": 60.0,
        "change_pct": 2.0,
        "relative_volume": 3.0,
        **overrides,
    }


def _generate(request):
    return {
        "model": request["actor"]["model"],
        "request_id": "generation-1",
        "forecasts": [
            {
                "ticker": row["ticker"],
                "target_price": 0.9 if row["ticker"] == "DOWN" else 1.1,
                "reason": "Saved momentum supports the target.",
            }
            for row in request["leaders"]
            if not row["pass_reason"]
        ],
    }


def _bar(
    ticker,
    close,
    *,
    interval="1d",
    bar_at=f"{DAY}T04:00:00+00:00",
    collected_at=f"{DAY}T20:16:00+00:00",
    source="yahoo",
):
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_bars(source,ticker,interval,bar_time,close,
                first_collected_at,last_collected_at) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(source,ticker,interval,bar_time) DO UPDATE SET
                close=excluded.close,last_collected_at=excluded.last_collected_at
            """,
            (source, ticker, interval, bar_at, close, collected_at, collected_at),
        )


def _forecasts():
    return {
        row["ticker"]: row["eod_forecast"] for row in market_report(DAY, "pre_market")["leaders"]
    }


def test_frozen_targets_use_saved_evidence_and_model(monkeypatch):
    _report()
    requests = []

    def generate(request):
        requests.append(request)

        assert generate_market_forecasts(_generate, PRE)["completed"] == 0
        return _generate(request)

    result = generate_market_forecasts(generate, PRE)
    assert result == {"completed": 1, "expired": 0, "failed": 0}
    assert len(requests) == 1
    assert requests[0]["evidence_as_of"] == PRE.isoformat()
    assert requests[0]["horizon"] == "the regular-session closing price on report_day"
    assert requests[0]["leaders"][0]["price"] == 1.0
    targets = _forecasts()
    assert targets["UP"]["target_price"] == 1.1
    assert targets["UP"]["direction"] == "up"
    assert targets["DOWN"]["direction"] == "down"
    assert targets["UP"]["model"] == FLASH.model
    assert targets["UP"]["contract_version"] == CONTRACT_VERSION
    assert targets["UP"]["status"] == "pending"
    assert generate_market_forecasts(generate, PRE)["completed"] == 0
    assert len(requests) == 1
    assert _forecasts() == targets
    assert market_reports_overview(PRE)["featured"]["forecast_state"] == "complete"


@pytest.mark.parametrize(
    "direction,close,expected",
    [
        ("UP", 1.1, "hit"),
        ("UP", 1.2, "hit"),
        ("UP", 1.0999, "miss"),
        ("DOWN", 0.9, "hit"),
        ("DOWN", 0.8, "hit"),
        ("DOWN", 0.9001, "miss"),
    ],
)
def test_eod_target_boundaries(direction, close, expected):
    _report(leaders=[_leader(direction)])
    generate_market_forecasts(_generate, PRE)
    _bar(direction, close)
    assert settle_market_forecasts(CLOSE, fetch_market_data=False)["resolved"] == 1
    receipt = _forecasts()[direction]
    assert receipt["status"] == expected
    assert receipt["close_price"] == close
    assert receipt["close_source"] == "yahoo"
    assert receipt["close_interval"] == "1d"
    assert receipt["close_collected_at"] == f"{DAY}T20:16:00+00:00"
    _bar(direction, 5.0)
    assert settle_market_forecasts(CLOSE, fetch_market_data=False)["resolved"] == 0
    assert _forecasts()[direction] == receipt


def test_only_same_session_completed_close_counts():
    _report(leaders=[_leader("UP")])
    generate_market_forecasts(_generate, PRE)
    _bar("UP", 1.5, interval="5m", bar_at=f"{DAY}T18:00:00+00:00")
    _bar("UP", 1.5, interval="5m", bar_at=f"{DAY}T20:05:00+00:00")
    _bar("UP", 1.5, bar_at="2026-09-03T04:00:00+00:00")
    _bar("UP", 1.5, bar_at="2026-09-01T04:00:00+00:00")
    _bar("UP", 1.5, collected_at=f"{DAY}T19:00:00+00:00")
    assert settle_market_forecasts(CLOSE, fetch_market_data=False)["resolved"] == 0
    assert _forecasts()["UP"]["status"] == "pending"
    _bar("UP", 1.05, interval="5m", bar_at=f"{DAY}T19:55:00+00:00")
    assert settle_market_forecasts(CLOSE, fetch_market_data=False)["resolved"] == 1
    assert _forecasts()["UP"]["status"] == "miss"


def test_daily_close_covers_early_sessions_and_waits_for_settlement_time():
    _report(leaders=[_leader("UP")])
    generate_market_forecasts(_generate, PRE)
    _bar("UP", 1.2)
    assert (
        settle_market_forecasts(CLOSE - timedelta(hours=1), fetch_market_data=False)["resolved"]
        == 0
    )
    assert settle_market_forecasts(CLOSE, fetch_market_data=False)["resolved"] == 1


def test_newly_fetched_close_is_used_in_the_same_pass(monkeypatch):
    _report(leaders=[_leader("UP")])
    generate_market_forecasts(_generate, PRE)

    class MarketData:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def daily(self, tickers):
            assert tickers == ["UP"]
            _bar("UP", 1.2, collected_at=(CLOSE + timedelta(seconds=1)).isoformat())

    monkeypatch.setattr(market_forecasts, "recording_market_data", lambda **_kwargs: MarketData())
    times = iter([0, 2])
    monkeypatch.setattr(market_forecasts.time, "monotonic", lambda: next(times))
    assert settle_market_forecasts(CLOSE)["resolved"] == 1


def test_data_gap_enters_review_after_grace():
    _report(leaders=[_leader("UP")])
    generate_market_forecasts(_generate, PRE)
    assert settle_market_forecasts(CLOSE, fetch_market_data=False)["reviewed"] == 0
    result = settle_market_forecasts(CLOSE + timedelta(days=8), fetch_market_data=False)
    assert result["reviewed"] == 1
    assert _forecasts()["UP"]["status"] == "review"
    assert _forecasts()["UP"]["close_price"] is None


def test_failed_fetch_uses_completed_archive(monkeypatch):
    _report(leaders=[_leader("UP")])
    generate_market_forecasts(_generate, PRE)
    _bar("UP", 1.2)

    def fail(**_kwargs):
        raise TimeoutError("source timeout")

    monkeypatch.setattr(market_forecasts, "recording_market_data", fail)
    assert settle_market_forecasts(CLOSE) == {"resolved": 1, "reviewed": 0, "fetch_failed": 1}


@pytest.mark.parametrize("price", [None, 0, -1, float("nan"), float("inf"), True])
def test_bad_reference_prices_receive_a_pass(price):
    _report(leaders=[_leader("UP", price=price)])
    assert generate_market_forecasts(None, PRE)["completed"] == 1
    assert _forecasts()["UP"]["status"] == "pass"


def test_risk_states_and_stale_prices_receive_a_pass():
    _report(
        leaders=[
            _leader("UP", trade_state="AVOID"),
            _leader("EXIT", trade_state="EXIT"),
            _leader("OLD", quote_time=f"{DAY}T10:00:00+00:00"),
        ]
    )
    assert generate_market_forecasts(None, PRE)["completed"] == 1
    assert all(row["status"] == "pass" for row in _forecasts().values())


def test_flash_can_pass_on_thin_evidence():
    _report(leaders=[_leader("UP")])

    def generate(request):
        result = _generate(request)
        result["forecasts"][0]["target_price"] = None
        return result

    assert generate_market_forecasts(generate, PRE)["completed"] == 1
    assert _forecasts()["UP"]["status"] == "pass"


@pytest.mark.parametrize(
    "change",
    [
        {"forecasts": []},
        {"model": "another-model"},
        {"forecasts": [{"ticker": "OTHER", "target_price": 1.1, "reason": "test"}]},
        {"forecasts": [{"ticker": "UP", "target_price": True, "reason": "test"}]},
        {"forecasts": [{"ticker": "UP", "target_price": float("inf"), "reason": "test"}]},
        {"forecasts": [{"ticker": "UP", "target_price": 1.0, "reason": "test"}]},
        {"forecasts": [{"ticker": "UP", "target_price": 1.1, "reason": ""}]},
        {"forecasts": [{"ticker": "UP", "reason": "test"}]},
    ],
)
def test_invalid_model_outputs_retry_with_a_fixed_limit(change):
    _report(leaders=[_leader("UP")])
    calls = []

    def generate(request):
        calls.append(request)
        return {**_generate(request), **change}

    for _ in range(5):
        generate_market_forecasts(generate, PRE)
    assert len(calls) == 3
    report = market_report(DAY, "pre_market")
    assert report["forecast_state"] == "failed"
    assert report["leaders"][0]["eod_forecast"] is None


def test_worker_restart_can_reclaim_an_expired_lease():
    _report(leaders=[_leader("UP")])
    with connection() as database:
        database.execute(
            "UPDATE market_report_forecast_jobs SET status='running',lease_until=?,attempts=1",
            ((PRE - timedelta(minutes=1)).isoformat(),),
        )
    assert generate_market_forecasts(_generate, PRE)["completed"] == 1


def test_slow_provider_response_expires_at_open(monkeypatch):
    _report(leaders=[_leader("UP")])
    times = iter([0, 120])
    monkeypatch.setattr(market_forecasts.time, "monotonic", lambda: next(times))
    at = datetime(2026, 9, 2, 13, 29, tzinfo=UTC)
    assert generate_market_forecasts(_generate, at)["expired"] == 1
    assert _forecasts()["UP"] is None


def test_legacy_and_late_reports_keep_their_original_history():
    _report(leaders=[_leader("UP")], queue=False)
    assert market_report(DAY, "pre_market")["forecast_state"] == "legacy"
    assert generate_market_forecasts(_generate, PRE)["completed"] == 0
    with connection() as database:
        queue_market_forecasts(database, "report", DAY, [_leader("UP")], CLOSE)
        assert (
            database.execute("SELECT COUNT(*) FROM market_report_forecast_jobs").fetchone()[0] == 0
        )


def test_expiry_at_open_and_server_key_wait():
    _report(leaders=[_leader("UP")])
    assert generate_market_forecasts(None, PRE)["completed"] == 0
    assert market_report(DAY, "pre_market")["forecast_state"] == "queued"
    at = datetime(2026, 9, 2, 13, 30, tzinfo=UTC)
    assert generate_market_forecasts(_generate, at)["expired"] == 1
    assert _forecasts()["UP"] is None


def test_corporate_action_enters_price_review():
    _report(leaders=[_leader("UP")])
    generate_market_forecasts(_generate, PRE)
    _bar("UP", 11.0)
    register_source(
        SourcePolicy(
            source="test",
            feed="actions",
            title="Actions",
            owner="Test",
            terms_url=None,
            credential_env=None,
            expected_cadence_seconds=None,
            stale_after_seconds=None,
            schedule="daily",
            storage_policy="allowed",
            display_policy="allowed",
            attribution=None,
            review_status="approved",
            enabled=True,
        )
    )
    with connection() as database:
        database.execute(
            """
            INSERT INTO ingestion_runs(id,source,feed,locator,status,started_at,finished_at,
                requested_count,received_count,content_hash,metadata_json)
            VALUES('run','test','actions','test://actions','success',?,?,1,1,'hash','{}')
            """,
            (CLOSE.isoformat(), CLOSE.isoformat()),
        )
        database.execute(
            """
            INSERT INTO market_events(source,feed,event_id,version,ticker,event_type,
                event_at,status,source_url,payload_json,first_run_id,last_run_id,
                first_collected_at,last_collected_at)
            VALUES('test','actions','split','1','UP','reverse_split',?,'active',
                'https://example.com','{}','run','run',?,?)
            """,
            (f"{DAY}T14:00:00+00:00", CLOSE.isoformat(), CLOSE.isoformat()),
        )
    assert settle_market_forecasts(CLOSE, fetch_market_data=False)["reviewed"] == 1
    assert _forecasts()["UP"]["status"] == "review"


@pytest.mark.parametrize(
    "stamp",
    [
        f"{DAY}T00:00:00-04:00",
        f"{DAY}T00:00:00+00:00",
        f"{DAY}T00:00:00",
    ],
)
def test_daily_session_dates_support_provider_timezones(stamp):
    _report(leaders=[_leader("UP")])
    generate_market_forecasts(_generate, PRE)
    _bar("UP", 1.2, bar_at=stamp)
    assert settle_market_forecasts(CLOSE, fetch_market_data=False)["resolved"] == 1


def test_report_page_shows_saved_targets_results_and_escaped_reasons(monkeypatch, tmp_path):
    from starlette.requests import Request

    from runner_web import main as web_main

    _report(
        leaders=[
            _leader("UP"),
            _leader("DOWN"),
            _leader("WAIT"),
            _leader("PASS", trade_state="AVOID"),
            *[_leader(f"MORE{i}") for i in range(4)],
        ]
    )

    def generate(request):
        result = _generate(request)
        result["forecasts"][0]["reason"] = "<script>alert('sample')</script>"
        return result

    generate_market_forecasts(generate, PRE)
    _bar("UP", 1.2)
    _bar("DOWN", 1.0)
    settle_market_forecasts(CLOSE, fetch_market_data=False)
    overview = market_reports_overview(CLOSE)
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
    for text in (
        "Target EOD",
        "Actual close",
        "$1.1000",
        "$0.9000",
        "$1.2000",
        "result-hit",
        "result-miss",
        "result-pending",
        "result-pass",
        "MORE3",
    ):
        assert text in html
    assert "&lt;script&gt;" in html
    assert "<script>alert" not in html
    assert "Close source: Yahoo" in html
    root = Path(__file__).parents[1]
    html = re.sub(
        r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
        lambda match: f"<style>{(root / 'web/static' / match.group(1)).read_text()}</style>",
        html,
    )
    html = re.sub(r'<script src="/static/[^"]+"[^>]*></script>', "", html)
    (tmp_path / "reports.html").write_text(html)


def test_openrouter_request_contains_the_approved_public_fields(monkeypatch):
    from runner_web import main as web_main

    request = {
        "actor": {"model": FLASH.model, "model_label": "GLM 5.3"},
        "contract_version": CONTRACT_VERSION,
        "report_day": DAY,
        "evidence_as_of": PRE.isoformat(),
        "horizon": "the regular-session closing price on report_day",
        "scoring": "up: close >= target; down: close <= target",
        "leaders": [
            {
                "ticker": "UP",
                "rank": 1,
                "score": 60.0,
                "price": 1.0,
                "change_pct": 2.0,
                "momentum_15m_pct": 1.0,
                "relative_volume": 3.0,
                "recent_relative_volume": 4.0,
                "signals": ["Volume acceleration"],
                "risks": ["Thin float"],
                "rug_score": 20.0,
                "trade_state": "WATCH",
                "quote_time": f"{DAY}T13:00:00+00:00",
                "pass_reason": None,
            },
            {**_leader("PASS", trade_state="AVOID"), "pass_reason": "Saved risk state."},
        ],
    }
    response = {
        "id": "generation-1",
        "model": FLASH.model,
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "forecasts": [
                                {
                                    "ticker": "UP",
                                    "target_price": 1.1,
                                    "reason": "Saved momentum supports the target.",
                                }
                            ]
                        }
                    )
                }
            }
        ],
    }
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self, limit):
            assert limit == 262_145
            return json.dumps(response).encode()

    def urlopen(api_request, *, timeout):
        assert timeout == 90
        captured["url"] = api_request.full_url
        captured["authorization"] = api_request.get_header("Authorization")
        captured["body"] = json.loads(api_request.data)
        return Response()

    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "server-key")
    monkeypatch.setattr(web_main.urllib.request, "urlopen", urlopen)
    result = web_main._generate_market_report_targets(request)

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["authorization"] == "Bearer server-key"
    sent = json.loads(captured["body"]["messages"][1]["content"])
    assert [row["ticker"] for row in sent["leaders"]] == ["UP"]
    assert set(sent["leaders"][0]) == {
        "ticker",
        "rank",
        "score",
        "price",
        "change_pct",
        "momentum_15m_pct",
        "relative_volume",
        "recent_relative_volume",
        "signals",
        "risks",
        "rug_score",
        "trade_state",
        "quote_time",
        "pass_reason",
    }
    assert result == {
        "forecasts": [
            {
                "ticker": "UP",
                "target_price": 1.1,
                "reason": "Saved momentum supports the target.",
            }
        ],
        "model": FLASH.model,
        "request_id": "generation-1",
    }
