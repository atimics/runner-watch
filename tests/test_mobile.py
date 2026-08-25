import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch
from starlette.requests import Request

from runner_watch.ingestion import MarketEvent, SourceBatch, SourceFetch
from runner_web import db
from runner_web import main as web_main
from runner_web.db import connection, init_db
from runner_web.ingestion import record_source_batch
from runner_web.main import (
    APP_ORIGIN,
    _chart_annotations,
    _commission_research,
    _evidence_gate,
    _market_trade_pressure,
    _pulse_entry_markers,
    _pulse_label,
    _record_activity,
    alpha_board_data,
    commissioned_reports,
    get_commission,
    heart_state,
    pulse_data,
    radar_data,
    register_options,
    templates,
    ticker_detail_data,
)


def test_passkey_signup_needs_no_profile_fields(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "auth.db")
    init_db()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/register/options",
            "headers": [(b"origin", APP_ORIGIN.encode())],
            "client": ("127.0.0.42", 4210),
        }
    )

    response = register_options(request)

    assert response.status_code == 200
    with connection() as database:
        user = database.execute("SELECT * FROM users").fetchone()
    assert user is not None
    assert user["username"].startswith("member_")
    assert user["display_name"] == "Member"


def insert_filing(
    accession: str,
    ticker: str,
    price: float,
    score: float,
    filed_at: str,
    transaction_codes: str = "",
) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                filing_url,transaction_codes,price,change_pct,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                accession,
                1,
                ticker,
                f"{ticker} Company",
                "4",
                "Insider open-market buy",
                "positive",
                score,
                f"4 - {ticker} Company",
                filed_at,
                f"https://www.sec.gov/{accession}",
                transaction_codes,
                price,
                6.5,
                filed_at,
                filed_at,
            ),
        )


def insert_scan_run(run_id: str, captured_at: str, candidate_rows: int) -> None:
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


def insert_scored_snapshot(
    snapshot_id: str,
    run_id: str,
    ticker: str,
    score: float,
    rank: int,
    captured_at: str,
    *,
    price: float = 1.25,
) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at,
                scan_run_id,baseline_rank
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                ticker,
                score,
                "BUILDING",
                "regular",
                price,
                8.0,
                2.0,
                4.0,
                3.0,
                4.0,
                0.8,
                800_000,
                captured_at,
                '["Volume acceleration"]',
                "[]",
                captured_at,
                run_id,
                rank,
            ),
        )


def test_pulse_only_lists_tickers_from_the_latest_scored_scan(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "mobile.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("pulse-run", captured_at, 2)
    insert_scored_snapshot("pulse-one", "pulse-run", "ONE", 42, 1, captured_at)
    insert_scored_snapshot("pulse-two", "pulse-run", "TWO", 31, 2, captured_at)
    insert_filing("one-event", "ONE", 1.25, 80, captured_at, "P")
    insert_filing("event-only", "EVENT", 2.25, 99, captured_at, "P")

    result = pulse_data()

    assert [row["ticker"] for row in result["rows"]] == ["ONE", "TWO"]
    assert result["rows"][0]["custom_score"] == 51.6
    assert result["rows"][0]["score_components"] == {
        "market": 42.0,
        "sec_event": 9.6,
        "news": 0.0,
        "social_search": 0.0,
        "community": 0.0,
        "safety": -0.0,
    }
    assert result["rows"][0]["event_count"] == 1
    assert result["rows"][0]["section"] == "scored"
    assert "EVENT" not in {row["ticker"] for row in result["rows"]}


def test_news_and_social_flow_into_pulse_radar_and_alpha(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "external-signals.db")
    init_db()
    timestamp = datetime.now(UTC)
    captured_at = timestamp.isoformat()
    insert_scan_run("external-run", captured_at, 1)
    insert_scored_snapshot("external-snapshot", "external-run", "FLOW", 40, 1, captured_at)
    with connection() as database:
        database.execute(
            """
            INSERT INTO ticker_hearts(profile_id,ticker,active,created_at,updated_at)
            VALUES(?,?,?,?,?)
            """,
            ("v:fan", "FLOW", 1, captured_at, captured_at),
        )
    fetch = SourceFetch.success(
        source="test_discovery",
        feed="mixed",
        locator="https://example.test/discovery",
        started_at=timestamp,
        payload={"ticker": "FLOW"},
        content_type="application/json",
    )
    record_source_batch(
        SourceBatch(
            fetch=fetch,
            market_events=(
                MarketEvent(
                    event_id="news",
                    ticker="FLOW",
                    event_type="news_article",
                    event_at=timestamp - timedelta(minutes=2),
                    published_at=timestamp - timedelta(minutes=2),
                    status="published",
                    source_url="https://news.example/flow",
                    payload={"title": "Flow Systems wins a new contract"},
                ),
                MarketEvent(
                    event_id="social",
                    ticker="FLOW",
                    event_type="social_spike",
                    event_at=timestamp - timedelta(minutes=1),
                    published_at=timestamp - timedelta(minutes=1),
                    status="active",
                    source_url="https://bsky.app/profile/example/post/one",
                    payload={
                        "mention_count": 4,
                        "engagement_count": 15,
                        "network_label": "Bluesky",
                    },
                ),
            ),
        )
    )

    pulse = pulse_data()["rows"][0]
    radar = radar_data(visitor_id="reader")[0]
    alpha = alpha_board_data("v:fan")["rows"][0]

    assert pulse["score_components"] == {
        "market": 40.0,
        "sec_event": 0.0,
        "news": 1.5,
        "social_search": 4.32,
        "community": 2.0,
        "safety": -0.0,
    }
    assert pulse["custom_score"] == 47.82
    assert pulse["external_social_mentions"] == 4
    assert pulse["news_count"] == 1
    assert radar["pulse_label"] == "Bluesky · 4 cashtag mentions"
    assert radar["filing_url"].startswith("https://bsky.app/")
    assert alpha["heart_count"] == 1
    assert alpha["external_social_mentions"] == 4
    assert alpha["news_count"] == 1
    assert alpha["pulse_label"] == "Bluesky · 4 cashtag mentions"


def test_ticker_detail_explains_form_four_purchase(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "detail.db")
    init_db()
    filed_at = datetime.now(UTC).isoformat()
    insert_filing("detail-one", "PEN", 1.75, 77, filed_at, "P")

    detail = ticker_detail_data("PEN")

    assert detail is not None
    assert detail["events"][0]["evidence_label"] == "Verified insider purchase"
    assert detail["events"][0]["pulse_label"] == "Form 4 · insider buy"


def test_pulse_label_does_not_call_a_sale_a_buy() -> None:
    assert _pulse_label({"transaction_codes": "S", "actor_title": "CEO"}) == (
        "Form 4 · insider sale"
    )


def test_pulse_puts_market_runners_before_filing_only_events(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ordered.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("filing-only", "FILE", 2.0, 99, captured_at, "P")
    insert_scan_run("runner-scan", captured_at, 1)
    with connection() as database:
        database.execute(
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) VALUES(?,?,?,?,?)",
            (1, "RUN", "Runner Systems", "NASDAQ", captured_at),
        )
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at,
                scan_run_id,baseline_rank
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "runner", "RUN", 24, "BUILDING", "regular", 1.25, 13.0, 2.0,
                4.0, 4.2, 5.0, 1.0, 800_000, captured_at,
                '["Volume acceleration"]', '["Wide spread risk"]', captured_at,
                "runner-scan", 1,
            ),
        )

    result = pulse_data()

    assert [row["ticker"] for row in result["rows"]] == ["RUN"]
    assert result["rows"][0]["source"] == "market"
    assert result["rows"][0]["company"] == "Runner Systems"
    assert result["rows"][0]["evidence_gate"]["state"] == "ready"


def test_trade_pressure_is_an_honest_bar_derived_estimate(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "pressure.db")
    init_db()
    with connection() as database:
        for index in range(12):
            database.execute(
                """
                INSERT INTO market_bars(
                    source,ticker,interval,bar_time,open,high,low,close,volume,
                    first_collected_at,last_collected_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "yahoo",
                    "BUY",
                    "5m",
                    f"2026-08-24T14:{index * 5:02d}:00+00:00",
                    10.0,
                    11.0,
                    9.0,
                    10.8,
                    1000 + index * 100,
                    "2026-08-24T15:00:00+00:00",
                    "2026-08-24T15:00:00+00:00",
                ),
            )

    pressure = _market_trade_pressure("BUY")

    assert pressure["available"] is True
    assert pressure["buy_pressure_pct"] == 90.0
    assert pressure["delta_volume"] > 0
    assert pressure["bar_count"] == 12
    assert "not bids" in pressure["note"]


def test_evidence_gate_only_opens_after_four_independent_checks() -> None:
    current = {
        "relative_volume": 3.0,
        "recent_relative_volume": 4.0,
        "momentum_15m_pct": 4.0,
        "momentum_acceleration_pct": 1.0,
        "vwap_position_pct": 1.0,
        "breakout_pct": 1.0,
    }

    gate = _evidence_gate(current, [])

    assert gate["state"] == "ready"
    assert gate["threshold"] == 4
    assert gate["count"] >= gate["threshold"]


def test_ticker_detail_prefers_market_state_and_uses_scan_outcome(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "detail-market.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("detail-filing", "PEN", 1.7, 88, captured_at, "P")
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "detail-snapshot", "PEN", 31, "EARLY", "regular", 1.9, 9.0, 1.1,
                3.3, 3.0, 4.0, 0.8, 900_000, captured_at,
                '["Fresh volume"]', '["Low float risk"]', captured_at,
            ),
        )
        database.execute(
            """
            INSERT INTO scan_outcomes(
                snapshot_id,ticker,base_price,base_at,return_1h_pct,updated_at
            ) VALUES(?,?,?,?,?,?)
            """,
            ("detail-snapshot", "PEN", 1.9, captured_at, 6.2, captured_at),
        )

    detail = ticker_detail_data("PEN")

    assert detail is not None
    assert detail["current"]["source"] == "market"
    assert detail["current"]["signals"] == ["Fresh volume"]
    assert detail["current"]["return_1h_pct"] == 6.2
    assert detail["events"][0]["evidence_label"] == "Verified insider purchase"


def test_radar_marks_new_state_seen(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "radar.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("radar-event", "RAD", 2.1, 82, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("user", "watcher", "Watcher", "active", captured_at),
        )
        database.execute(
            "INSERT INTO watches(user_id,ticker,created_at,last_seen_at) VALUES(?,?,?,NULL)",
            ("user", "RAD", captured_at),
        )
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "radar-snapshot", "RAD", 22, "EARLY", "regular", 2.1, 7.0, 1.0,
                2.0, 2.5, 3.0, 0.5, 500_000, captured_at, "[]", "[]", captured_at,
            ),
        )

    first = radar_data("user", mark_seen=True)
    second = radar_data("user")

    assert first[0]["has_update"] is True
    assert first[0]["source"] == "sec"
    assert first[0]["price"] == 2.1
    assert first[0]["evidence_gate"]["checks"] == ["Positive SEC catalyst"]
    assert second[0]["has_update"] is False


def test_radar_uses_filing_price_when_a_market_snapshot_is_missing(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "radar-filing.db")
    init_db()
    filed_at = datetime.now(UTC).isoformat()
    insert_filing("radar-filing", "FILE", 0.72, 73, filed_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("user", "watcher", "Watcher", "active", filed_at),
        )
        database.execute(
            "INSERT INTO watches(user_id,ticker,created_at,last_seen_at) VALUES(?,?,?,NULL)",
            ("user", "FILE", filed_at),
        )

    result = radar_data("user")

    assert result[0]["source"] == "sec"
    assert result[0]["price"] == 0.72
    assert result[0]["change_pct"] == 6.5
    assert result[0]["evidence_gate"]["checks"] == ["Positive SEC catalyst"]


def test_alpha_ranks_unique_hearts(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "alpha.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("alpha-one", "ONE", 1.25, 70, captured_at, "P")
    insert_filing("alpha-two", "TWO", 2.50, 80, captured_at, "P")
    with connection() as database:
        database.executemany(
            """
            INSERT INTO ticker_hearts(profile_id,ticker,active,created_at,updated_at)
            VALUES(?,?,?,?,?)
            """,
            [
                ("v:first", "ONE", 1, captured_at, captured_at),
                ("v:second", "ONE", 1, captured_at, captured_at),
                ("v:first", "TWO", 1, captured_at, captured_at),
            ],
        )
        database.execute(
            """
            INSERT INTO alpha_reports(
                id,ticker,evidence_key,status,model,headline,summary,
                catalysts_json,risks_json,watch_json,sources_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "report-one",
                "ONE",
                "evidence",
                "complete",
                "test-model",
                "ONE leads Alpha",
                "Verified evidence summary.",
                '["Insider purchase"]',
                '["Low liquidity"]',
                '["Volume"]',
                '["https://www.sec.gov/alpha-one"]',
                captured_at,
                captured_at,
            ),
        )

    board = alpha_board_data("v:first")

    assert [row["ticker"] for row in board["rows"]] == ["ONE", "TWO"]
    assert board["rows"][0]["heart_count"] == 2
    assert board["rows"][0]["is_leader"] is True
    assert board["rows"][0]["hearted"] is True
    assert board["rows"][0]["ai_report"]["catalysts"] == ["Insider purchase"]
    assert heart_state("ONE", "v:second") == {"ticker": "ONE", "count": 2, "hearted": True}

    request = Request({"type": "http", "method": "GET", "path": "/community", "headers": []})
    request.state.csp_nonce = "test"
    template = templates.get_template("community.html")
    free_html = template.render(
        request=request,
        board=board,
        user=None,
        is_subscriber=False,
        active_tab="alpha",
    )
    subscriber_html = template.render(
        request=request,
        board=board,
        user={"username": "member", "display_name": "Member"},
        is_subscriber=True,
        active_tab="alpha",
    )
    assert "Subscriber report ready" in free_html
    assert "Verified evidence summary." not in free_html
    assert "Verified evidence summary." in subscriber_html


def test_radar_orders_events_by_time_not_activity(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "automatic-radar.db")
    init_db()
    captured_at = datetime.now(UTC)
    insert_filing("activity-one", "ONE", 1.25, 90, captured_at.isoformat(), "P")
    insert_filing(
        "activity-two",
        "TWO",
        2.50,
        60,
        (captured_at - timedelta(minutes=5)).isoformat(),
        "P",
    )
    _record_activity("v:device", "TWO", "share")

    result = radar_data(visitor_id="device")

    assert result
    assert result[0]["ticker"] == "ONE"
    assert result[0]["section"] == "events"


def test_pulse_supports_cursor_style_offsets(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "pulse-pages.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("page-run", captured_at, 2)
    insert_scored_snapshot("page-one", "page-run", "ONE", 90, 1, captured_at)
    insert_scored_snapshot("page-two", "page-run", "TWO", 80, 2, captured_at)

    first = pulse_data(limit=1)
    second = pulse_data(offset=1, limit=1)

    assert len(first["rows"]) == 1
    assert first["has_more"] is True
    assert first["next_offset"] == 1
    assert second["rows"][0]["ticker"] != first["rows"][0]["ticker"]


def test_chart_annotations_keep_the_real_pulse_entry_and_detected_events(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "chart-annotations.db")
    init_db()
    timestamp = datetime.now(UTC)
    before_entry = (timestamp - timedelta(minutes=30)).isoformat()
    entered_at = (timestamp - timedelta(minutes=20)).isoformat()
    refreshed_at = (timestamp - timedelta(minutes=10)).isoformat()
    insert_scan_run("before-entry", before_entry, 1)
    insert_scored_snapshot("before-two", "before-entry", "TWO", 25, 1, before_entry)
    insert_scan_run("entry", entered_at, 1)
    insert_scored_snapshot("entry-one", "entry", "ONE", 50, 1, entered_at, price=1.4)
    insert_scan_run("refresh", refreshed_at, 1)
    insert_scored_snapshot("refresh-one", "refresh", "ONE", 55, 1, refreshed_at, price=1.6)
    filing_at = (timestamp - timedelta(minutes=16)).isoformat()
    insert_filing("chart-filing", "ONE", 1.45, 80, filing_at, "P")
    media_at = timestamp - timedelta(minutes=12)
    fetch = SourceFetch.success(
        source="test_media",
        feed="social",
        locator="https://example.test/media",
        started_at=media_at,
        payload={"ticker": "ONE"},
        content_type="application/json",
    )
    record_source_batch(
        SourceBatch(
            fetch=fetch,
            market_events=(
                MarketEvent(
                    event_id="one-media-spike",
                    ticker="ONE",
                    event_type="social_spike",
                    event_at=media_at,
                    published_at=media_at,
                    status="active",
                    source_url="https://example.test/media/one",
                    payload={"mention_count": 6, "engagement_count": 18},
                ),
            ),
        )
    )

    entry = _pulse_entry_markers(["ONE"])["ONE"]
    annotations = _chart_annotations(["ONE"])["ONE"]

    assert entry["time"] == entered_at
    assert entry["price"] == 1.4
    assert {item["type"] for item in annotations} == {
        "pulse_entry",
        "edgar_filing",
        "media_spike",
    }
    assert next(item for item in annotations if item["type"] == "media_spike")[
        "label"
    ] == "Media spike · 6 mentions"


def test_commissioned_report_is_public_without_storing_the_openrouter_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "commission.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("commission-one", "ONE", 1.25, 90, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("commissioner", "member_commission", "Member", "active", captured_at),
        )
    monkeypatch.setattr(
        web_main,
        "_generate_openrouter_report",
        lambda key, evidence, user_id: (
            {
                "headline": "ONE evidence report",
                "summary": "A source-bound summary.",
                "catalysts": ["Verified insider purchase"],
                "risks": ["Low liquidity"],
                "watch": ["Volume"],
            },
            "test/research-model",
            {"total_tokens": 321},
        ),
    )

    report = _commission_research("commissioner", "ONE", "sk-or-device-only-test-key")

    assert report["headline"] == "ONE evidence report"
    assert get_commission(report["public_id"])["summary"] == "A source-bound summary."
    assert commissioned_reports()[0]["ticker"] == "ONE"
    assert alpha_board_data("v:reader")["commissions"][0]["public_id"] == report["public_id"]
    with connection() as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(research_commissions)")}
        stored = database.execute("SELECT * FROM research_commissions").fetchone()
    assert "openrouter_key" not in columns
    assert "sk-or-device-only-test-key" not in " ".join(str(value) for value in stored)


def test_commission_request_uses_glm_53_with_a_minimal_prompt(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    generated = {
        "headline": "ONE evidence report",
        "summary": "A source-bound summary.",
        "catalysts": ["Verified insider purchase"],
        "risks": ["Low liquidity"],
        "watch": ["Volume"],
    }

    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        captured["body"] = json.loads(request.data)
        return io.BytesIO(
            json.dumps(
                {
                    "choices": [{"message": {"content": json.dumps(generated)}}],
                    "model": "z-ai/glm-5.3",
                    "usage": {"total_tokens": 123},
                }
            ).encode()
        )

    monkeypatch.setattr(web_main.urllib.request, "urlopen", fake_urlopen)
    report, model, usage = web_main._generate_openrouter_report(
        "sk-or-test-key-long-enough", {"ticker": "ONE"}, "member-one"
    )

    body = captured["body"]
    assert body["model"] == "z-ai/glm-5.3"
    assert body["response_format"] == {"type": "json_object"}
    assert body["provider"] == {"require_parameters": True}
    assert body["reasoning_effort"] == "low"
    assert "temperature" not in body
    assert len(body["messages"][0]["content"].split()) <= 30
    assert report == generated
    assert model == "z-ai/glm-5.3"
    assert usage == {"total_tokens": 123}


def test_research_report_template_has_public_share_metadata(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "share-report.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("share-one", "ONE", 1.25, 90, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("sharer", "member_sharer", "Member", "active", captured_at),
        )
    monkeypatch.setattr(
        web_main,
        "_generate_openrouter_report",
        lambda key, evidence, user_id: (
            {
                "headline": "Shareable ONE report",
                "summary": "Built from stored evidence.",
                "catalysts": [],
                "risks": [],
                "watch": [],
            },
            "test/model",
            {},
        ),
    )
    report = _commission_research("sharer", "ONE", "sk-or-share-test-key")
    request = Request({"type": "http", "method": "GET", "path": "/research", "headers": []})
    request.state.csp_nonce = "test"

    html = templates.get_template("research_report.html").render(
        request=request,
        report=report,
        app_origin="https://stonks.example",
        user=None,
        active_tab="alpha",
    )

    assert "Shareable ONE report" in html
    assert f"/research/{report['public_id']}/card.png" in html
    assert "sk-or-share-test-key" not in html
