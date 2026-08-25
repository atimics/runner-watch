import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch
from starlette.requests import Request

from runner_watch.ingestion import MarketEvent, SourceBatch, SourceFetch
from runner_web import db
from runner_web import main as web_main
from runner_web.db import connection, init_db
from runner_web.ingestion import record_source_batch
from runner_web.main import (
    APP_ORIGIN,
    PulseAttentionItem,
    _chart_annotations,
    _commission_research,
    _evidence_gate,
    _market_trade_pressure,
    _previous_trade_states,
    _pulse_entry_markers,
    _pulse_label,
    _record_activity,
    _stored_market_risk_contexts,
    _write_pulse_attention,
    alpha_board_data,
    commissioned_reports,
    get_commission,
    heart_state,
    pulse_data,
    pulse_notification_data,
    radar_data,
    register_options,
    templates,
    ticker_charts_payload,
    ticker_detail_data,
)


def test_pulse_and_radar_refresh_affordances_have_separate_jobs() -> None:
    root = Path(__file__).parents[1]
    pulse_template = (root / "web/templates/pulse.html").read_text()
    radar_template = (root / "web/templates/radar.html").read_text()

    assert "1 new ticker" in pulse_template
    assert "new tickers" in pulse_template
    assert "Pulse updated" not in pulse_template
    assert "TickerRow.fingerprint" not in pulse_template
    assert "New since you looked" not in pulse_template
    assert "exposureQueue" in pulse_template
    assert "body:JSON.stringify({entries})" in pulse_template
    assert "1 new event" in radar_template
    assert "pendingUpdateTickers" in radar_template


def test_ui_copy_drops_ai_and_corporate_filler() -> None:
    root = Path(__file__).parents[1]
    ui_copy = "\n".join(
        path.read_text()
        for folder in (root / "web/templates", root / "web/static")
        for path in folder.glob("*")
        if path.suffix in {".html", ".js"}
    )

    banned = (
        "AI KOL",
        "IMMUTABLE PAPER CALLS",
        "honest abandon",
        "Filings decoded",
        "one shot from",
        "Subscriber report ready",
        "safe to leave this page",
        "Outcome clock running",
        "Starting SEC listener",
        "before the crowd",
        "Publish permanent card",
    )
    for phrase in banned:
        assert phrase not in ui_copy


def test_ticker_rows_use_color_without_new_or_seen_tags() -> None:
    root = Path(__file__).parents[1]
    row_script = (root / "web/static/ticker-row.js").read_text()
    row_styles = (root / "web/static/ticker-row.css").read_text()

    assert "attention-unseen" in row_styles
    assert "attention-seen" in row_styles
    assert "attention-badge" not in row_script
    assert "new to you" not in row_script
    assert "seen but not opened" not in row_script
    assert "return ['NEW', 'update']" not in row_script


def test_passkey_signup_needs_no_profile_fields(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
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


def test_ticker_page_renders_guest_flash_attribution(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ticker-page.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("ticker-page-run", captured_at, 1)
    insert_scored_snapshot(
        "ticker-page-snapshot",
        "ticker-page-run",
        "ONE",
        42,
        1,
        captured_at,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/t/ONE",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 4210),
            "scheme": "http",
            "server": ("testserver", 80),
            "root_path": "",
        }
    )
    request.state.visitor_id = "visitor-abcdefghijklmnopqrstuv"

    response = web_main.ticker_page("ONE", request, None)

    assert response.status_code == 200
    assert "Uses GLM 5.3 through OpenRouter" in response.body.decode()


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


def test_pulse_reuses_the_shared_base_payload(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "pulse-cache.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("cached-run", captured_at, 1)
    insert_scored_snapshot("cached-one", "cached-run", "ONE", 42, 1, captured_at)
    web_main.PULSE_DATA_CACHE.clear()
    original = web_main._pulse_data_uncached
    calls = 0

    def counted() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(web_main, "_pulse_data_uncached", counted)

    assert pulse_data(profile="v:first")["rows"][0]["ticker"] == "ONE"
    assert pulse_data(profile="v:second")["rows"][0]["ticker"] == "ONE"
    assert calls == 1


def test_radar_reuses_the_shared_base_payload(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "radar-cache.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("radar-cache-one", "ONE", 1.25, 80, captured_at, "P")
    web_main.RADAR_DATA_CACHE.clear()
    original = web_main._radar_base_data_uncached
    calls = 0

    def counted() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(web_main, "_radar_base_data_uncached", counted)

    assert radar_data(visitor_id="first")[0]["ticker"] == "ONE"
    assert radar_data(visitor_id="second")[0]["ticker"] == "ONE"
    assert calls == 1


def test_alpha_reuses_shared_data_but_keeps_profile_hearts(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "alpha-cache.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("alpha-cache-one", "ONE", 1.25, 80, captured_at, "P")
    with connection() as database:
        database.execute(
            """
            INSERT INTO ticker_hearts(profile_id,ticker,active,created_at,updated_at)
            VALUES(?,?,?,?,?)
            """,
            ("v:first", "ONE", 1, captured_at, captured_at),
        )
    web_main.ALPHA_DATA_CACHE.clear()
    original = web_main._alpha_base_data_uncached
    calls = 0

    def counted() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(web_main, "_alpha_base_data_uncached", counted)

    first = alpha_board_data("v:first")
    second = alpha_board_data("v:second")

    assert first["rows"][0]["hearted"] is True
    assert second["rows"][0]["hearted"] is False
    assert calls == 1


def test_pulse_entry_moves_from_unseen_to_seen_to_inspected(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "pulse-attention.db")
    init_db()
    timestamp = datetime.now(UTC)
    before = (timestamp - timedelta(minutes=20)).isoformat()
    entered_at = (timestamp - timedelta(minutes=10)).isoformat()
    refreshed_at = (timestamp - timedelta(minutes=5)).isoformat()
    insert_scan_run("attention-before", before, 1)
    insert_scored_snapshot("attention-two", "attention-before", "TWO", 40, 1, before)
    insert_scan_run("attention-entry", entered_at, 1)
    insert_scored_snapshot("attention-one", "attention-entry", "ONE", 60, 1, entered_at)
    insert_scan_run("attention-refresh", refreshed_at, 1)
    insert_scored_snapshot("attention-one-refresh", "attention-refresh", "ONE", 62, 1, refreshed_at)
    item = PulseAttentionItem(ticker="ONE", entered_at=entered_at)

    row = pulse_data(profile="v:reader")["rows"][0]
    assert row["entered_at"] == entered_at
    assert row["novelty_state"] == "unseen"
    assert row["rug_score"] is None
    assert [entry["ticker"] for entry in pulse_notification_data("v:reader")["entries"]] == ["ONE"]

    assert _write_pulse_attention("v:reader", [item], "notified") == 1
    assert pulse_notification_data("v:reader")["entries"] == []
    assert pulse_data(profile="v:reader")["rows"][0]["novelty_state"] == "unseen"

    assert _write_pulse_attention("v:reader", [item], "seen") == 1
    assert pulse_data(profile="v:reader")["rows"][0]["novelty_state"] == "seen"

    assert _write_pulse_attention("v:reader", [item], "inspected") == 1
    assert pulse_data(profile="v:reader")["rows"][0]["novelty_state"] == "inspected"


def test_a_reentry_creates_a_new_unseen_pulse_episode(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "pulse-reentry.db")
    init_db()
    timestamp = datetime.now(UTC)
    first_entry = (timestamp - timedelta(minutes=30)).isoformat()
    absent = (timestamp - timedelta(minutes=20)).isoformat()
    reentry = (timestamp - timedelta(minutes=10)).isoformat()
    insert_scan_run("first-entry", first_entry, 1)
    insert_scored_snapshot("first-one", "first-entry", "ONE", 55, 1, first_entry)
    insert_scan_run("absent", absent, 1)
    insert_scored_snapshot("absent-two", "absent", "TWO", 50, 1, absent)
    _write_pulse_attention(
        "v:reader",
        [PulseAttentionItem(ticker="ONE", entered_at=first_entry)],
        "inspected",
    )
    insert_scan_run("reentry", reentry, 1)
    insert_scored_snapshot("reentry-one", "reentry", "ONE", 65, 1, reentry)

    row = pulse_data(profile="v:reader")["rows"][0]

    assert row["entered_at"] == reentry
    assert row["novelty_state"] == "unseen"


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
    assert detail["events"][0]["evidence_label"] == "Insider purchase"
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
                "runner",
                "RUN",
                24,
                "BUILDING",
                "regular",
                1.25,
                13.0,
                2.0,
                4.0,
                4.2,
                5.0,
                1.0,
                800_000,
                captured_at,
                '["Volume acceleration"]',
                '["Wide spread risk"]',
                captured_at,
                "runner-scan",
                1,
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
    assert "not live order flow" in pressure["note"]


def test_sparklines_use_ingested_bars_without_a_live_price_request(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "charts.db")
    init_db()
    collected_at = datetime.now(UTC).isoformat()
    with connection() as database:
        for index, close in enumerate((1.0, 1.1, 1.25)):
            database.execute(
                """
                INSERT INTO market_bars(
                    source,ticker,interval,bar_time,open,high,low,close,volume,
                    first_collected_at,last_collected_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "yahoo",
                    "SPRK",
                    "5m",
                    (datetime.now(UTC) - timedelta(minutes=10 - index * 5)).isoformat(),
                    close,
                    close,
                    close,
                    close,
                    1000,
                    collected_at,
                    collected_at,
                ),
            )

    monkeypatch.setattr(
        web_main,
        "recording_market_data",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("sparkline reads must not call Yahoo")
        ),
    )

    payload = ticker_charts_payload(["SPRK"])

    assert [point["price"] for point in payload["charts"]["SPRK"]] == [1.0, 1.1, 1.25]
    assert payload["freshness"]["SPRK"]["source"] == "yahoo"


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


def test_stored_halt_must_be_recently_confirmed(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "halt-risk.db")
    init_db()
    timestamp = datetime.now(UTC)
    fetch = SourceFetch.success(
        source="test_halts",
        feed="trade_halts",
        locator="https://example.test/halts",
        started_at=timestamp,
        payload={"tickers": ["FRESH", "STALE"]},
        content_type="application/json",
    )
    record_source_batch(
        SourceBatch(
            fetch=fetch,
            market_events=tuple(
                MarketEvent(
                    event_id=ticker.lower(),
                    ticker=ticker,
                    event_type="trading_halt",
                    event_at=timestamp - timedelta(days=3),
                    published_at=timestamp - timedelta(days=3),
                    status="active",
                    source_url=f"https://example.test/halts/{ticker.lower()}",
                )
                for ticker in ("FRESH", "STALE")
            ),
        )
    )
    with connection() as database:
        database.execute(
            "UPDATE market_events SET last_collected_at=? WHERE ticker='STALE'",
            ((timestamp - timedelta(days=2)).isoformat(),),
        )
        contexts = _stored_market_risk_contexts(database, ["FRESH", "STALE"])

    assert contexts["FRESH"]["active_halt"] is True
    assert contexts["STALE"]["active_halt"] is False


def test_market_risk_context_ignores_news_payloads(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "risk-events.db")
    init_db()
    timestamp = datetime.now(UTC)
    fetch = SourceFetch.success(
        source="test_risk_events",
        feed="events",
        locator="https://example.test/risk-events",
        started_at=timestamp,
        payload={"ticker": "RISK"},
        content_type="application/json",
    )
    record_source_batch(
        SourceBatch(
            fetch=fetch,
            market_events=(
                MarketEvent(
                    event_id="irrelevant-news",
                    ticker="RISK",
                    event_type="news_article",
                    event_at=timestamp,
                    status="published",
                    source_url="https://example.test/news",
                    payload={"description": "Reverse split mentioned in an article"},
                ),
                MarketEvent(
                    event_id="real-split",
                    ticker="RISK",
                    event_type="corporate_action",
                    event_at=timestamp,
                    status="complete",
                    source_url="https://example.test/action",
                    payload={"description": "1-for-20 reverse split"},
                ),
            ),
        )
    )

    with connection() as database:
        context = _stored_market_risk_contexts(database, ["RISK"])["RISK"]

    assert context == {"active_halt": False, "reverse_split_count_1y": 1}


def test_critical_rug_risk_blocks_ready_and_lowers_pulse_rank(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "rug-rank.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("rug-run", captured_at, 2)
    insert_scored_snapshot("rugged", "rug-run", "RUG", 90, 1, captured_at)
    insert_scored_snapshot("clean", "rug-run", "CLEAN", 60, 2, captured_at)
    with connection() as database:
        database.execute(
            """
            UPDATE scan_snapshots SET setup_score=90,rug_score=90,rug_level='CRITICAL',
                trade_state='AVOID',hard_veto=1 WHERE id='rugged'
            """
        )
        database.execute(
            """
            UPDATE scan_snapshots SET setup_score=60,rug_score=10,rug_level='LOW',
                trade_state='TRIGGERED' WHERE id='clean'
            """
        )

    rows = pulse_data()["rows"]

    assert [row["ticker"] for row in rows] == ["CLEAN", "RUG"]
    assert rows[1]["evidence_gate"]["state"] == "blocked"
    assert rows[1]["score"] < rows[1]["setup_score"]


def test_previous_trade_states_returns_latest_state_with_index(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "previous-state.db")
    init_db()
    older = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    newer = datetime.now(UTC).isoformat()
    insert_scan_run("older-state-run", older, 1)
    insert_scored_snapshot("older-state", "older-state-run", "STATE", 20, 1, older)
    insert_scan_run("newer-state-run", newer, 1)
    insert_scored_snapshot("newer-state", "newer-state-run", "STATE", 30, 1, newer)
    with connection() as database:
        database.execute("UPDATE scan_snapshots SET trade_state='WATCH' WHERE id='older-state'")
        database.execute("UPDATE scan_snapshots SET trade_state='TRIGGERED' WHERE id='newer-state'")
        states = _previous_trade_states(database, ["STATE", "MISSING"])
        index_sql = database.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='scan_snapshots_ticker_state_captured'"
        ).fetchone()["sql"]

    assert states == {"STATE": "TRIGGERED"}
    assert "ticker,captured_at DESC,trade_state" in index_sql


def test_risk_filing_subtracts_attention_instead_of_boosting_it(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "risk-event.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("risk-run", captured_at, 1)
    insert_scored_snapshot("risk-row", "risk-run", "DILU", 50, 1, captured_at)
    insert_filing("risk-filing", "DILU", 1.0, 80, captured_at, "")
    with connection() as database:
        database.execute(
            """
            UPDATE sec_filings SET form='S-3',kind='Offering or dilution filing',
                sentiment='risk' WHERE accession='risk-filing'
            """
        )

    row = pulse_data()["rows"][0]

    assert row["event_boost"] == -20.0
    assert row["score"] == 30.0


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
                "detail-snapshot",
                "PEN",
                31,
                "EARLY",
                "regular",
                1.9,
                9.0,
                1.1,
                3.3,
                3.0,
                4.0,
                0.8,
                900_000,
                captured_at,
                '["Fresh volume"]',
                '["Low float risk"]',
                captured_at,
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
    assert detail["events"][0]["evidence_label"] == "Insider purchase"


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
                "radar-snapshot",
                "RAD",
                22,
                "EARLY",
                "regular",
                2.1,
                7.0,
                1.0,
                2.0,
                2.5,
                3.0,
                0.5,
                500_000,
                captured_at,
                "[]",
                "[]",
                captured_at,
            ),
        )

    first = radar_data("user", mark_seen=True)
    second = radar_data("user")

    assert first[0]["has_update"] is True
    assert first[0]["source"] == "sec"
    assert first[0]["price"] == 2.1
    assert first[0]["evidence_gate"]["checks"] == ["Positive SEC filing"]
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
    assert result[0]["evidence_gate"]["checks"] == ["Positive SEC filing"]


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
    assert "Subscribers only" in free_html
    assert "Verified evidence summary." not in free_html
    assert "Verified evidence summary." in subscriber_html


def test_radar_orders_events_by_time_not_activity(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
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
    assert (
        next(item for item in annotations if item["type"] == "media_spike")["label"]
        == "Social spike · 6 mentions"
    )


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
        lambda key, evidence, user_id, **kwargs: (
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
    assert report["thesis"] == "A source-bound summary."
    assert report["research_mode"] == "one_shot_system_context"
    assert report["actor"]["id"] == "kol-flash"
    assert report["actor"]["display_name"] == "Flash"
    assert report["actor"]["model"] == "z-ai/glm-5.3"
    assert report["actor"]["ladder_position"] == 1
    assert report["actor"]["ladder_size"] == 4
    assert get_commission(report["public_id"])["summary"] == "A source-bound summary."
    assert commissioned_reports()[0]["ticker"] == "ONE"
    assert alpha_board_data("v:reader")["commissions"][0]["public_id"] == report["public_id"]
    with connection() as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(research_commissions)")}
        stored = database.execute("SELECT * FROM research_commissions").fetchone()
    assert "openrouter_key" not in columns
    assert "sk-or-device-only-test-key" not in " ".join(str(value) for value in stored)

    same_report, created = web_main._create_research_commission("commissioner", "ONE")
    assert created is False
    assert same_report["public_id"] == report["public_id"]
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM research_commissions").fetchone()[0] == 1


def test_running_flash_commission_is_a_single_server_job(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "running-commission.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("running-one", "ONE", 1.25, 90, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("runner", "member_runner", "Member", "active", captured_at),
        )

    first, first_created = web_main._create_research_commission("runner", "ONE")
    second, second_created = web_main._create_research_commission("runner", "ONE")

    assert first_created is True
    assert second_created is False
    assert first["status"] == "running"
    assert second["public_id"] == first["public_id"]
    assert web_main.latest_commission("runner", "ONE")["status"] == "running"
    with connection() as database:
        rows = database.execute("SELECT * FROM research_commissions").fetchall()
    assert len(rows) == 1
    assert "openrouter" not in " ".join(rows[0].keys()).lower()


def test_flash_commission_page_resumes_the_server_job() -> None:
    template = (Path(__file__).parents[1] / "web/templates/ticker.html").read_text()

    assert "You can leave this page" in template
    assert "Retry Flash" in template
    assert "commissionNeedsRetry = result.retryable !== false" in template
    assert "fetch(`/api/research/${encodeURIComponent(ticker)}`)" in template
    assert "if (initialCommissionStatus === 'running') pollCommission()" in template
    assert "View report" in template


def test_commission_request_uses_glm_53_with_a_minimal_prompt(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    generated = {
        "headline": "ONE evidence report",
        "thesis": "ONE has a mixed evidence setup that still needs confirmation.",
        "summary": "A source-bound summary.",
        "company_profile": {"what_it_does": "Test company", "source_urls": []},
        "people": [],
        "filings": [],
        "catalysts": ["Verified insider purchase"],
        "risks": ["Low liquidity"],
        "watch": ["Volume"],
        "unknowns": ["Financing terms"],
        "sources": [],
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
    assert body["messages"][0]["content"].startswith("You are Flash")
    request_payload = json.loads(body["messages"][1]["content"])
    assert request_payload["actor"]["id"] == "kol-flash"
    assert request_payload["actor"]["model"] == "z-ai/glm-5.3"
    assert body["response_format"] == {"type": "json_object"}
    assert body["provider"] == {"require_parameters": True}
    assert body["reasoning_effort"] == "high"
    assert "temperature" not in body
    assert "tools" not in body
    assert "plugins" not in body
    assert len(body["messages"][0]["content"].split()) <= 30
    assert report == generated
    assert model == "z-ai/glm-5.3"
    assert usage["total_tokens"] == 123
    assert usage["generation"]["normalized_fields"] == []
    assert usage["generation"]["content_chars"] > 0


def test_commission_normalizes_recoverable_glm_output(
    monkeypatch: MonkeyPatch,
) -> None:
    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        return io.BytesIO(
            json.dumps(
                {
                    "id": "generation-safe-id",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "summary": "EU has attention, but the setup needs proof.",
                                        "risks": [{"text": "Financing terms are unclear."}],
                                    }
                                )
                            },
                        }
                    ],
                    "model": "z-ai/glm-5.3",
                }
            ).encode()
        )

    monkeypatch.setattr(web_main.urllib.request, "urlopen", fake_urlopen)
    report, _, usage = web_main._generate_openrouter_report(
        "sk-or-test-key-long-enough", {"ticker": "EU"}, "member-eu"
    )

    assert report["headline"] == "EU has attention, but the setup needs proof"
    assert report["thesis"] == report["summary"]
    assert report["company_profile"] == {"source_urls": []}
    assert report["people"] == []
    assert report["filings"] == []
    assert report["risks"] == ["Financing terms are unclear."]
    assert "thesis" in usage["generation"]["normalized_fields"]
    assert usage["generation"]["finish_reason"] == "stop"


def test_commission_reports_cut_off_glm_output_without_storing_content(
    monkeypatch: MonkeyPatch,
) -> None:
    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        return io.BytesIO(
            json.dumps(
                {
                    "id": "cut-off-safe-id",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": '{"summary":"EU started but'},
                        }
                    ],
                    "model": "z-ai/glm-5.3",
                    "usage": {"completion_tokens": 12000},
                }
            ).encode()
        )

    monkeypatch.setattr(web_main.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(web_main.ReportGenerationFailure) as failure:
        web_main._generate_openrouter_report(
            "sk-or-test-key-long-enough", {"ticker": "EU"}, "member-eu"
        )

    assert failure.value.detail == "Flash ran out of room before finishing the report. Retry Flash."
    assert failure.value.diagnostics["finish_reason"] == "length"
    assert failure.value.diagnostics["completion_tokens"] == 12000
    assert failure.value.diagnostics["content_chars"] > 0
    assert "EU started" not in json.dumps(failure.value.diagnostics)


def test_failed_commission_stores_safe_diagnostics_and_is_retryable(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "failed-commission.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("failed-eu", "EU", 1.25, 90, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("failed-user", "member_failed", "Member", "active", captured_at),
        )

    def fail_report(*args: Any, **kwargs: Any) -> Any:
        raise web_main.ReportGenerationFailure(
            502,
            "Flash returned a malformed report. Retry Flash.",
            {
                "phase": "provider_response",
                "finish_reason": "length",
                "content_chars": 812,
                "failure_kind": "invalid_json",
            },
        )

    monkeypatch.setattr(web_main, "_generate_openrouter_report", fail_report)
    with pytest.raises(web_main.ReportGenerationFailure):
        _commission_research("failed-user", "EU", "sk-or-device-only-test-key")

    failed = web_main.latest_commission("failed-user", "EU")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["usage"]["failure"]["failure_kind"] == "invalid_json"
    assert web_main._commission_api_payload(failed)["retryable"] is True
    assert "sk-or-device-only-test-key" not in json.dumps(failed)
    retry, created = web_main._create_research_commission("failed-user", "EU")
    assert created is True
    assert retry["status"] == "running"


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
        lambda key, evidence, user_id, **kwargs: (
            {
                "headline": "Shareable ONE report",
                "thesis": "ONE has a source-bound watch thesis.",
                "summary": "Built from stored evidence.",
                "company_profile": {},
                "people": [],
                "filings": [],
                "catalysts": [],
                "risks": [],
                "watch": [],
                "unknowns": [],
                "sources": [],
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
    assert "<strong>Flash</strong>" in html
    assert "#1 of 4 · GLM 5.3" in html
    assert f"/research/{report['public_id']}/card.png" in html
    assert "sk-or-share-test-key" not in html
