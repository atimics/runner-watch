import asyncio
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request

from runner_watch.ingestion import MarketEvent, SourceBatch, SourceFetch
from runner_web import db
from runner_web import main as web_main
from runner_web.db import connection, init_db
from runner_web.flash_wallet import claim_daily_flash, wallet_for_user
from runner_web.ingestion import record_source_batch
from runner_web.main import (
    APP_ORIGIN,
    _chart_annotations,
    _commission_research,
    _evidence_gate,
    _market_trade_pressure,
    _previous_trade_states,
    _pulse_entry_markers,
    _pulse_label,
    _record_pulse_entries_for_run,
    _stored_market_risk_contexts,
    alpha_board_data,
    alpha_comments_data,
    comments_for_ticker,
    create_ticker_comment,
    get_commission,
    prune_storage,
    publish_signal,
    pulse_data,
    radar_data,
    register_options,
    scan_collection_allowed,
    templates,
    ticker_chart_detail_payload,
    ticker_charts_payload,
    ticker_detail_data,
)
from runner_web.pseudonyms import COMMENT_AVATAR_ABILITIES


def _test_flash_forecast() -> dict[str, Any]:
    return {
        "direction": "no_call",
        "probability_up": 0.5,
        "reason": "The frozen evidence does not support a directional call.",
    }


def _approve_test_source(source: str, feed: str) -> None:
    with connection() as database:
        database.execute(
            """
            UPDATE source_registry
            SET review_status='approved',display_policy='source_link_with_attribution',enabled=1
            WHERE source=? AND feed=?
            """,
            (source, feed),
        )


@pytest.mark.parametrize(
    ("probabilities", "expected_return", "direction", "label", "arrow"),
    [
        ((0.62, 0.18, 0.20), 2.4, "up", "Upside setup", "↑"),
        ((0.19, 0.58, 0.23), -1.1, "down", "Downside pressure", "↓"),
        ((0.34, 0.32, 0.34), 1.3, "flat", "No edge", "↔"),
        ((0.35, 0.31, 0.34), 1.7, "flat", "No edge", "↔"),
        ((0.40, 0.48, 0.12), 1.3, "up", "Upside setup", "↑"),
    ],
)
def test_ranker_directional_thesis_uses_the_three_way_contract(
    probabilities: tuple[float, float, float],
    expected_return: float,
    direction: str,
    label: str,
    arrow: str,
) -> None:
    up, down, timeout = probabilities

    thesis = web_main._ranker_directional_thesis(
        {
            "probability_up": up,
            "probability_down": down,
            "probability_timeout": timeout,
            "expected_return_pct": expected_return,
            "model_id": "ranker-test",
            "model_status": "shadow",
            "created_at": "2026-08-28T18:00:00+00:00",
        }
    )

    assert thesis is not None
    assert thesis["direction"] == direction
    assert thesis["label"] == label
    assert thesis["arrow"] == arrow
    assert thesis["horizon"] == "60m"
    assert thesis["contract"] == "+8% before −4% within 60 minutes"
    assert thesis["status_label"] == "Shadow model"
    assert thesis["model_id"] == "ranker-test"
    assert [item["key"] for item in thesis["distribution"]] == [
        "down",
        "timeout",
        "up",
    ]
    assert sum(item["probability_pct"] for item in thesis["distribution"]) == 100


def test_ticker_model_path_is_compact_explicit_and_separate_from_risk() -> None:
    root = Path(__file__).parents[1]
    template = (root / "web/templates/ticker.html").read_text()
    styles = (root / "web/static/ticker-row.css").read_text()

    assert 'class="model-path-card model-path-' in template
    assert "MODEL PATH ·" in template
    assert "detail.directional_thesis.distribution" in template
    assert "No barrier” means neither was reached" in template
    assert template.index('class="model-path-card') < template.index('class="risk-decision')
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in styles
    assert ".model-path-segment.path-down { background: var(--red); }" in styles
    assert ".model-path-segment.path-up { background: var(--green); }" in styles


def test_pulse_and_radar_refresh_affordances_have_separate_jobs() -> None:
    root = Path(__file__).parents[1]
    pulse_template = (root / "web/templates/pulse.html").read_text()
    radar_template = (root / "web/templates/radar.html").read_text()

    assert "1 new ticker" in pulse_template
    assert "new tickers" in pulse_template
    assert "Pulse updated" not in pulse_template
    assert "TickerRow.fingerprint" not in pulse_template
    assert "New since you looked" not in pulse_template
    assert "exposureQueue" not in pulse_template
    assert "body:JSON.stringify({entries})" not in pulse_template
    assert "1 new event" in radar_template
    assert "RatiLiveList.mount" in pulse_template
    assert "RatiLiveList.mount" in radar_template
    assert "pendingUpdateTickers" not in radar_template


def test_pulse_does_not_render_an_empty_scorecard_spacer() -> None:
    root = Path(__file__).parents[1]
    pulse_template = (root / "web/templates/pulse.html").read_text()
    kol_styles = (root / "web/static/kol.css").read_text()

    assert 'id="kolScoreStrip"' in pulse_template
    assert "{% if not flash_record %} hidden{% endif %}" in pulse_template
    assert ".kol-score-strip[hidden] { display: none; }" in kol_styles
    assert "function renderKolScorecard()" in pulse_template
    assert "pulse.flash_record = next.flash_record || null;" in pulse_template


def test_pulse_refresh_handles_missing_markers_stale_updates_and_page_failures() -> None:
    pulse_template = (Path(__file__).parents[1] / "web/templates/pulse.html").read_text()

    assert "function tickerKey(row)" in pulse_template
    assert "flatMap(row => [tickerKey(row), entryKey(row)])" in pulse_template
    assert "RatiLiveList.mount" in pulse_template
    assert "pendingPulse" not in pulse_template
    assert "function scheduleLoadMoreRetry()" in pulse_template
    assert "catch (_) {\n    scheduleLoadMoreRetry();" in pulse_template


def test_large_responses_are_compressed() -> None:
    assert any(middleware.cls is GZipMiddleware for middleware in web_main.app.user_middleware)


def test_ticker_has_public_call_and_flash_actions() -> None:
    template = (Path(__file__).parents[1] / "web/templates/ticker.html").read_text()
    script = (Path(__file__).parents[1] / "web/static/ticker-detail.js").read_text()
    comments = (
        Path(__file__).parents[1] / "web/templates/_flash_comments.html"
    ).read_text()
    comment_script = (
        Path(__file__).parents[1] / "web/static/flash-comments.js"
    ).read_text()
    action = (
        Path(__file__).parents[1] / "web/templates/_flash_report_action.html"
    ).read_text()

    assert "Track a thesis" not in template
    assert "thesisDialog" not in template
    assert "thesisForm" not in template
    assert "flash_report_action(flash_report)" in template
    assert "commissionButton" in action
    assert "Make Call" in template
    assert 'class="ticker-action-row"' in template
    assert template.count('class="ticker-action-slot"') == 2
    assert "action-card" not in template
    assert template.count("<textarea") == 0
    assert "flash_comments('stock', detail.ticker" in template
    assert 'id="generateComment"' in comments
    assert "Post with Flash" in comments
    assert "Persistent avatars · public across tickers" not in template
    assert "ability guides a short Flash draft" not in template
    assert "Start the read" not in template
    assert "Flash drafted" not in template
    assert "Flash is drafting" not in script
    assert "comment_generation_enabled" in template
    assert "window.RatiFlash?.canSpend" in comment_script
    assert "render_comment_avatar(comment.avatar)" in comments
    assert "'Idempotency-Key': pending" in comment_script
    assert "sessionStorage.setItem(storageKey" in comment_script
    assert "item.dataset.commentId === String(result.comment.id)" in comment_script
    assert "const RECOVERY_DELAYS = [500, 1500, 3000, 5000]" in comment_script
    assert "Still posting. Tap again." in comment_script


def test_ticker_layout_puts_subtle_actions_after_the_analysis() -> None:
    root = Path(__file__).parents[1]
    template = (root / "web/templates/ticker.html").read_text()
    desktop_css = (root / "web/static/desktop-split.css").read_text()

    chart = template.index('class="detail-chart-panel"')
    actions = template.index('class="detail-actions"')
    analysis = template.index('class="detail-analysis"')

    assert chart < analysis < actions
    assert 'grid-template-areas:' in desktop_css
    assert '"chart"\n      "analysis"\n      "actions"' in desktop_css
    assert "grid-template-columns: repeat(2, minmax(0, 210px))" in desktop_css
    assert "background: rgba(255, 255, 255, .018)" in desktop_css
    assert "min-height: 44px" in desktop_css


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            {"comment": "My view is cautious; low volume is the risk."},
            "My view is cautious; low volume is the risk.",
        ),
        (
            json.dumps({"answer": "My read is mixed; financing is the risk."}),
            "My read is mixed; financing is the risk.",
        ),
        (
            [{"type": "text", "text": "My setup is early; dilution is the risk."}],
            "My setup is early; dilution is the risk.",
        ),
        (
            "```json\n{\"comment\":\"My signal is improving; liquidity is the risk.\"}\n```",
            "My signal is improving; liquidity is the risk.",
        ),
        (
            "Comment: My evidence is limited; volatility is the risk.",
            "My evidence is limited; volatility is the risk.",
        ),
    ],
)
def test_openrouter_comment_accepts_supported_response_shapes(
    content: Any, expected: str
) -> None:
    assert web_main._openrouter_comment_text(content) == expected


def test_openrouter_comment_stays_short_and_rejects_missing_text() -> None:
    long_comment = "My evidence remains mixed, and I am watching the next filing. " * 8

    comment = web_main._openrouter_comment_text(long_comment)

    assert len(comment) <= web_main.COMMENT_MAX_CHARS
    assert long_comment.startswith(comment)
    with pytest.raises(ValueError):
        web_main._openrouter_comment_text({"unexpected": "not a comment"})
    with pytest.raises(ValueError):
        web_main._openrouter_comment_text("   ")


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


def test_scanner_page_is_removed() -> None:
    root = Path(__file__).parents[1]
    routes = {
        path
        for route in web_main.app.routes
        if (path := getattr(route, "path", None)) is not None
    }
    templates_text = "\n".join(
        path.read_text() for path in (root / "web/templates").glob("*.html")
    )

    assert "/scanner" not in routes
    assert "/api/scan" not in routes
    assert 'href="/scanner"' not in templates_text
    assert not (root / "web/templates/scanner.html").exists()


def test_desktop_feeds_share_full_info_and_article_panel() -> None:
    root = Path(__file__).parents[1]
    templates_dir = root / "web/templates"
    pulse = (templates_dir / "pulse.html").read_text()
    radar = (templates_dir / "radar.html").read_text()
    alpha = (templates_dir / "community.html").read_text()
    panel = (templates_dir / "_desktop_panel.html").read_text()
    workspace = (root / "web/static/desktop-workspace.js").read_text()
    desktop_css = (root / "web/static/desktop-split.css").read_text()

    for template in (pulse, radar, alpha):
        assert "workspace-app" in template
        assert "data-desktop-workspace" in template
        assert "data-desktop-list" in template
        assert '{% include "_desktop_panel.html" %}' in template
    assert "data-desktop-frame" in panel
    assert "data-desktop-loading" in panel
    assert "/t/" in workspace
    assert "/research/" in workspace
    assert "frame.addEventListener('load'" in workspace
    assert "frame.hidden = true" in workspace
    assert ".desktop-detail-loading[hidden]" in desktop_css
    assert "openPanel(current ? panelUrl(current) : defaultUrl())" in workspace
    assert "sessionStorage" not in workspace
    assert "html.embedded-pane .mobile-app" in desktop_css
    assert '"SAMEORIGIN" if panel_path else "DENY"' in (
        root / "src/runner_web/main.py"
    ).read_text()
    assert '"/game/"' in (root / "src/runner_web/main.py").read_text()


def test_desktop_panel_security_allows_only_supported_detail_pages() -> None:
    for path in (
        "/t/WRAP",
        "/research/report-1",
        "/game/mlb:401816699",
        "/sports/game/mlb:401816699",
    ):
        assert web_main._is_panel_path(path)

    for path in ("/", "/radar", "/sports", "/sports/alpha", "/api/sports/pulse"):
        assert not web_main._is_panel_path(path)


def test_sports_pages_use_the_runners_shell_and_workspace_contract() -> None:
    root = Path(__file__).parents[1]
    templates_dir = root / "web/templates"
    sports_templates = [
        (templates_dir / name).read_text()
        for name in ("sports.html", "sports_radar.html", "sports_alpha.html")
    ]
    game = (templates_dir / "sports_game.html").read_text()
    live_script = (root / "web/static/sports-live.js").read_text()
    product_styles = (root / "web/static/sports-product.css").read_text()

    for template in sports_templates:
        assert '{% extends "mobile_base.html" %}' in template
        assert "workspace-app" in template
        assert "data-desktop-workspace" in template
        assert "data-desktop-list" in template
        assert '{% include "_desktop_panel.html" %}' in template
        assert "sports_base.html" not in template
    assert '{% extends "mobile_base.html" %}' in game
    assert 'class="detail-nav sports-detail-nav"' in game
    assert 'class="detail-body sports-detail-body"' in game
    assert 'class="game-detail-grid game-detail-flow"' in game
    assert "html:not(.embedded-pane) .game-detail-grid" in product_styles
    assert ".game-detail-grid.game-detail-flow" in product_styles
    assert "max-width: 1120px" in product_styles
    assert not (templates_dir / "sports_base.html").exists()
    assert "location.reload" not in "\n".join([*sports_templates, game, live_script])
    assert "RatiLiveList.mount" in live_script
    assert "setInterval(poll" not in live_script
    assert "body.sports-product" in product_styles
    assert not product_styles.startswith(":root")
    assert (templates_dir / "_sports_styles.html").read_text().count("stylesheet") == 1


def test_ticker_rows_have_no_reader_attention_state() -> None:
    root = Path(__file__).parents[1]
    row_script = (root / "web/static/ticker-row.js").read_text()
    row_styles = (root / "web/static/ticker-row.css").read_text()

    assert "attention-unseen" not in row_styles
    assert "attention-seen" not in row_styles
    assert "novelty_state" not in row_script
    assert "data-novelty" not in row_script
    assert "attention-badge" not in row_script
    assert "new to you" not in row_script
    assert "seen but not opened" not in row_script
    assert "return ['NEW', 'update']" not in row_script
    assert "tradeState !== statusLabel" in row_script


def test_pulse_ticker_search_uses_native_validation() -> None:
    root = Path(__file__).parents[1]
    pulse_template = (root / "web/templates/pulse.html").read_text()
    account_strip = (root / "web/templates/_account_strip.html").read_text()

    assert 'pattern="[A-Za-z0-9.-]{1,12}"' in account_strip
    assert " required " in account_strip
    assert 'id="pulseSearch"' not in pulse_template
    assert '{% include "_market_clock.html" %}' not in pulse_template
    assert "Movement is only the first clue." not in pulse_template


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


def test_rate_limit_keys_do_not_contain_the_ip_or_account_id(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[str] = []
    monkeypatch.setattr(web_main, "RATE_LIMIT_HASH_KEY", b"test-rate-limit-key")
    monkeypatch.setattr(
        web_main,
        "rate_limit_allowed",
        lambda key, limit, seconds: captured.append(key) or True,
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/pulse",
            "headers": [],
            "client": ("203.0.113.42", 4200),
        }
    )

    web_main.enforce_rate(request, "public", limit=1, seconds=60)
    web_main.enforce_rate(
        request,
        "account",
        limit=1,
        seconds=60,
        subject="private-user-id",
    )

    assert len(captured) == 2
    assert all("203.0.113.42" not in key for key in captured)
    assert all("private-user-id" not in key for key in captured)


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
    quote_time: str | None = None,
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
                quote_time or captured_at,
                '["Volume acceleration"]',
                "[]",
                captured_at,
                run_id,
                rank,
            ),
        )
        _record_pulse_entries_for_run(database, run_id, captured_at)


@pytest.mark.parametrize(
    ("timestamp", "allowed"),
    [
        (datetime(2026, 8, 31, 7, 59, tzinfo=UTC), False),
        (datetime(2026, 8, 31, 8, 0, tzinfo=UTC), True),
        (datetime(2026, 8, 31, 23, 59, tzinfo=UTC), True),
        (datetime(2026, 9, 1, 0, 0, tzinfo=UTC), False),
        (datetime(2026, 8, 30, 14, 0, tzinfo=UTC), False),
    ],
)
def test_scan_collection_only_runs_during_the_weekday_market_window(
    timestamp: datetime,
    allowed: bool,
) -> None:
    assert scan_collection_allowed(timestamp) is allowed


def test_storage_prune_removes_old_raw_snapshots_but_keeps_public_receipts(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "retention.db")
    init_db()
    captured_at = (datetime.now(UTC) - timedelta(days=200)).isoformat()
    insert_scan_run("old-unreferenced-run", captured_at, 1)
    insert_scored_snapshot(
        "old-unreferenced-snapshot",
        "old-unreferenced-run",
        "OLD",
        20,
        1,
        captured_at,
    )
    insert_scan_run("old-public-run", captured_at, 1)
    insert_scored_snapshot(
        "old-public-snapshot",
        "old-public-run",
        "KEPT",
        40,
        1,
        captured_at,
    )
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("receipt-user", "receipt_user", "Receipt User", "active", captured_at),
        )
        database.execute(
            """
            INSERT INTO signals(
                id,public_id,snapshot_id,user_id,thesis,horizon,
                invalidation,disclosure,status,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "receipt-signal",
                "receipt-public",
                "old-public-snapshot",
                "receipt-user",
                "Public receipt",
                "intraday",
                "Invalidated",
                "No position",
                "public",
                captured_at,
            ),
        )

    prune_storage()

    with connection() as database:
        snapshots = {
            str(row["id"])
            for row in database.execute("SELECT id FROM scan_snapshots").fetchall()
        }
        runs = {
            str(row["id"])
            for row in database.execute("SELECT id FROM scan_runs").fetchall()
        }
    assert snapshots == {"old-public-snapshot"}
    assert runs == {"old-public-run"}


def test_human_written_public_signals_are_retired() -> None:
    with pytest.raises(HTTPException) as error:
        publish_signal()

    assert error.value.status_code == 410
    assert error.value.detail == "Public Signals were replaced by Calls."


def test_ticker_page_does_not_add_a_guest_research_action(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ticker-page.db")
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "")
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
    response = web_main.ticker_page("ONE", request, None)

    assert response.status_code == 200
    assert "Ask Flash" not in response.body.decode()
    assert "Report unavailable" in response.body.decode()
    assert "Log in to generate" not in response.body.decode()
    assert "Connect OpenRouter" not in response.body.decode()


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


def test_pulse_freshness_uses_the_market_quote_time(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "quote-freshness.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    quote_time = (datetime.now(UTC) - timedelta(hours=53)).isoformat()
    insert_scan_run("quote-run", captured_at, 1)
    insert_scored_snapshot(
        "quote-snapshot",
        "quote-run",
        "OLD",
        42,
        1,
        captured_at,
        quote_time=quote_time,
    )

    result = pulse_data()

    assert result["updated_at"] == quote_time
    assert result["market_updated_at"] == quote_time
    assert result["rows"][0]["quote_time"] == quote_time


def test_pulse_reuses_the_shared_base_payload(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "pulse-cache.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("cached-run", captured_at, 1)
    insert_scored_snapshot("cached-one", "cached-run", "ONE", 42, 1, captured_at)
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    original = web_main._pulse_data_uncached
    calls = 0

    def counted() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(web_main, "_pulse_data_uncached", counted)

    assert pulse_data()["rows"][0]["ticker"] == "ONE"
    assert pulse_data()["rows"][0]["ticker"] == "ONE"
    assert calls == 1


def test_pulse_coalesces_concurrent_first_build(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "pulse-single-flight.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("single-flight-run", captured_at, 1)
    insert_scored_snapshot(
        "single-flight-one",
        "single-flight-run",
        "ONE",
        42,
        1,
        captured_at,
    )
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    web_main.PUBLIC_SCREEN_DATA_REFRESHING.clear()
    original = web_main._pulse_data_uncached
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def counted() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return original()

    monkeypatch.setattr(web_main, "_pulse_data_uncached", counted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pulse_data)
        assert started.wait(timeout=2)
        second = pool.submit(pulse_data)
        release.set()
        assert first.result(timeout=2)["rows"][0]["ticker"] == "ONE"
        assert second.result(timeout=2)["rows"][0]["ticker"] == "ONE"

    assert calls == 1


def test_chart_payload_caches_points_and_annotations_together(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "chart-payload-cache.db")
    web_main.CHART_PAYLOAD_CACHE.clear()
    web_main.CHART_PAYLOAD_REFRESHING.clear()
    calls = 0
    payload = {
        "charts": {"ONE": [{"price": 1.25}]},
        "freshness": {"ONE": {"status": "fresh"}},
        "annotations": {"ONE": [{"type": "pulse_entry"}]},
    }

    def counted(requested: list[str]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert requested == ["ONE"]
        return payload

    monkeypatch.setattr(web_main, "_ticker_charts_payload_uncached", counted)

    assert ticker_charts_payload(["ONE"]) == payload
    assert ticker_charts_payload(["ONE"]) == payload
    assert calls == 1


def test_list_chart_payload_sends_only_time_and_price(monkeypatch: MonkeyPatch) -> None:
    snapshot = SimpleNamespace(
        data=[
            {
                "time": "2026-08-26T12:00:00+00:00",
                "price": 1.25,
                "open": 1.20,
                "high": 1.30,
                "low": 1.18,
                "volume": 50_000,
                "vwap": 1.23,
            }
        ],
        metadata=lambda: {"status": "fresh"},
    )
    monkeypatch.setattr(
        web_main,
        "ticker_chart_snapshots",
        lambda requested: {"ONE": snapshot},
    )
    monkeypatch.setattr(web_main, "_chart_annotations", lambda requested: {"ONE": []})

    payload = web_main._compact_list_chart_payload(
        web_main._ticker_charts_payload_uncached(["ONE"])
    )

    assert payload["charts"] == {
        "ONE": [{"time": "2026-08-26T12:00:00+00:00", "price": 1.25}]
    }


def test_radar_reuses_the_shared_base_payload(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "radar-cache.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("radar-cache-one", "ONE", 1.25, 80, captured_at, "P")
    insert_scan_run("radar-cache-run", captured_at, 1)
    insert_scored_snapshot(
        "radar-cache-snapshot", "radar-cache-run", "ONE", 60, 1, captured_at
    )
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    original = web_main._radar_base_data_uncached
    calls = 0

    def counted() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(web_main, "_radar_base_data_uncached", counted)

    assert radar_data()[0]["ticker"] == "ONE"
    assert radar_data()[0]["ticker"] == "ONE"
    assert calls == 1


def test_alpha_reuses_shared_public_call_data(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "alpha-cache.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("alpha-cache-one", "ONE", 1.25, 80, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("caller", "caller", "Caller", "active", captured_at),
        )
        database.execute(
            "INSERT INTO caller_identities("
            "id,handle,user_id,status,claim_cost_cents,claimed_at) "
            "VALUES(?,?,?,'active',0,?)",
            ("caller-id", "green-wolf", "caller", captured_at),
        )
        database.execute(
            """INSERT INTO community_calls(
                id,public_id,user_id,caller_identity_id,ticker,entry_price,entry_at,
                status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'active',?,?)""",
            (
                "call", "public-call", "caller", "caller-id", "ONE", 1.25,
                captured_at, captured_at, captured_at,
            ),
        )
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    original = web_main._alpha_base_data_uncached
    calls = 0

    def counted() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(web_main, "_alpha_base_data_uncached", counted)

    first = alpha_board_data()
    second = alpha_board_data()

    assert first["rows"][0]["active_calls"] == 1
    assert second["rows"] == first["rows"]
    assert calls == 1


def test_pulse_entry_is_shared_without_storing_reader_attention(
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
    row = pulse_data()["rows"][0]
    assert row["entered_at"] == entered_at
    assert "novelty_state" not in row
    with connection() as database:
        assert database.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='pulse_profile_state'"
        ).fetchone() is None


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
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("fan", "fan", "Fan", "active", captured_at),
        )
        database.execute(
            "INSERT INTO caller_identities("
            "id,handle,user_id,status,claim_cost_cents,claimed_at) "
            "VALUES(?,?,?,'active',0,?)",
            ("fan-caller-id", "green-wolf", "fan", captured_at),
        )
        database.execute(
            """INSERT INTO community_calls(
                id,public_id,user_id,caller_identity_id,ticker,entry_price,entry_at,
                status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'active',?,?)""",
            (
                "flow-call", "flow-public", "fan", "fan-caller-id", "FLOW", 1.0,
                captured_at, captured_at, captured_at,
            ),
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
    _approve_test_source("test_discovery", "mixed")

    pulse = pulse_data()["rows"][0]
    radar = radar_data()[0]
    alpha = alpha_board_data()["rows"][0]

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
    assert alpha["active_calls"] == 1
    assert alpha["comment_count"] == 0
    assert alpha["external_social_mentions"] == 4
    assert alpha["news_count"] == 1
    assert alpha["pulse_label"] == "Bluesky · 4 cashtag mentions"


def test_negative_social_counts_do_not_break_pulse_or_radar(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "negative-social-counts.db")
    init_db()
    timestamp = datetime.now(UTC)
    captured_at = timestamp.isoformat()
    insert_scan_run("negative-social-run", captured_at, 1)
    insert_scored_snapshot(
        "negative-social-snapshot",
        "negative-social-run",
        "SAFE",
        40,
        1,
        captured_at,
    )
    fetch = SourceFetch.success(
        source="test_discovery",
        feed="social",
        locator="https://example.test/discovery",
        started_at=timestamp,
        payload={"ticker": "SAFE"},
        content_type="application/json",
    )
    record_source_batch(
        SourceBatch(
            fetch=fetch,
            market_events=(
                MarketEvent(
                    event_id="negative-social",
                    ticker="SAFE",
                    event_type="social_spike",
                    event_at=timestamp,
                    published_at=timestamp,
                    status="active",
                    source_url="https://example.test/social/safe",
                    payload={
                        "mention_count": -3,
                        "engagement_count": -8,
                        "network_label": "Social",
                    },
                ),
            ),
        )
    )
    _approve_test_source("test_discovery", "social")
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()

    pulse = pulse_data()["rows"][0]
    radar = radar_data()[0]

    assert pulse["external_social_mentions"] == 0
    assert pulse["score_components"]["social_search"] == 0.0
    assert radar["ticker"] == "SAFE"
    assert radar["external_social_mentions"] == 0
    assert radar["external_social_engagement"] == 0


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
    assert result["rows"][0]["evidence_gate"]["state"] == "gathering"
    assert result["rows"][0]["evidence_gate"]["checks"] == ["Market structure"]


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
    detail = ticker_chart_detail_payload("SPRK")

    assert [point["price"] for point in payload["charts"]["SPRK"]] == [1.0, 1.1, 1.25]
    assert all(
        {"open", "high", "low", "close", "volume", "vwap"} <= point.keys()
        for point in payload["charts"]["SPRK"]
    )
    assert payload["freshness"]["SPRK"]["source"] == "yahoo"
    assert detail["modes"]["astrology"].startswith("Fixed-anchor Fibonacci")
    assert detail["points"][-1]["close"] == 1.25


def test_evidence_gate_counts_market_calculations_as_one_family() -> None:
    current = {
        "relative_volume": 3.0,
        "recent_relative_volume": 4.0,
        "momentum_15m_pct": 4.0,
        "momentum_acceleration_pct": 1.0,
        "vwap_position_pct": 1.0,
        "breakout_pct": 1.0,
    }

    gate = _evidence_gate(current, [])

    assert gate["state"] == "gathering"
    assert gate["threshold"] == 3
    assert gate["count"] == 1
    assert gate["checks"] == ["Market structure"]
    assert len(gate["raw_market_checks"]) == 6


def test_evidence_gate_opens_with_three_independent_families() -> None:
    current = {
        "relative_volume": 3.0,
        "momentum_15m_pct": 4.0,
        "trade_state": "TRIGGERED",
    }
    events = [{"sentiment": "positive"}]
    external = {"news_count": 2, "social_mentions": 8}

    gate = _evidence_gate(current, events, external_context=external)

    assert gate["state"] == "ready"
    assert gate["count"] == 3
    assert gate["checks"] == [
        "Market structure",
        "Primary filing",
        "News coverage",
        "Crowd activity",
    ]


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
    _approve_test_source("test_halts", "trade_halts")
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
    _approve_test_source("test_risk_events", "events")

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
    insert_scan_run("radar-run", captured_at, 1)
    insert_scored_snapshot("radar-snapshot", "radar-run", "RAD", 22, 1, captured_at, price=2.1)
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("user", "watcher", "Watcher", "active", captured_at),
        )
        database.execute(
            "INSERT INTO watches(user_id,ticker,created_at,last_seen_at) VALUES(?,?,?,NULL)",
            ("user", "RAD", captured_at),
        )

    first = radar_data()
    second = radar_data()

    assert first[0]["has_update"] is True
    assert first[0]["source"] == "sec"
    assert first[0]["price"] == 2.1
    assert first[0]["evidence_gate"]["checks"] == ["Primary filing"]
    assert second[0]["has_update"] is True
    with connection() as database:
        assert database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='radar_seen'"
        ).fetchone() is None


def test_radar_excludes_events_for_tickers_not_in_pulse(
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

    result = radar_data()

    assert result == []


def test_alpha_ranks_public_calls_and_shows_pnl(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "alpha.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("alpha-one", "ONE", 1.25, 70, captured_at, "P")
    insert_filing("alpha-two", "TWO", 2.50, 80, captured_at, "P")
    with connection() as database:
        database.executemany(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            [
                ("caller-one", "caller_one", "Caller One", "active", captured_at),
                ("caller-two", "caller_two", "Caller Two", "active", captured_at),
                ("caller-three", "caller_three", "Caller Three", "active", captured_at),
                ("commenter", "commenter", "Commenter", "active", captured_at),
            ],
        )
        database.executemany(
            "INSERT INTO caller_identities("
            "id,handle,user_id,status,claim_cost_cents,claimed_at) "
            "VALUES(?,?,?,'active',0,?)",
            [
                ("identity-one", "green-wolf", "caller-one", captured_at),
                ("identity-two", "quiet-wolf", "caller-two", captured_at),
                ("identity-three", "bright-wolf", "caller-three", captured_at),
            ],
        )
        database.executemany(
            """INSERT INTO community_calls(
                id,public_id,user_id,caller_identity_id,ticker,entry_price,entry_at,
                status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'active',?,?)""",
            [
                (
                    "call-one", "public-one", "caller-one", "identity-one", "ONE", 1.0,
                    captured_at, captured_at, captured_at,
                ),
                (
                    "call-two", "public-two", "caller-two", "identity-two", "ONE", 1.2,
                    captured_at, captured_at, captured_at,
                ),
                (
                    "call-three", "public-three", "caller-three", "identity-three", "TWO", 2.0,
                    captured_at, captured_at, captured_at,
                ),
            ],
        )
        database.execute(
            """
            INSERT INTO ticker_comments(id,ticker,user_id,body,status,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            ("alpha-comment", "ONE", "commenter", "Watching ONE", "public", captured_at),
        )

    board = alpha_board_data()

    assert [row["ticker"] for row in board["rows"]] == ["ONE", "TWO"]
    assert board["rows"][0]["active_calls"] == 2
    assert board["rows"][0]["total_calls"] == 2
    assert board["rows"][0]["comment_count"] == 1
    assert board["rows"][0]["engagement_count"] == 4
    assert board["rows"][0]["is_leader"] is True
    assert board["calls"][0]["caller_handle"].endswith("wolf")
    assert board["total_calls"] == 3
    assert board["total_comments"] == 1

def test_ticker_feedback_tracks_signed_in_public_comments(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "feedback.db")
    init_db()
    timestamp = datetime.now(UTC)
    insert_filing("feedback-one", "ONE", 1.25, 70, timestamp.isoformat(), "P")

    raw_session = "feedback-session"
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("feedback-user", "chartreader", "Chart Reader", "active", timestamp.isoformat()),
        )
        database.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (
                web_main.token_hash(raw_session),
                "feedback-user",
                timestamp.isoformat(),
                (timestamp + timedelta(days=1)).isoformat(),
            ),
        )
    claim_daily_flash("feedback-user")
    generation_calls = 0

    def generate_comment(ticker: str, *, avatar_ability_id: str) -> tuple[str, str]:
        nonlocal generation_calls
        generation_calls += 1
        return (
            "My read is above VWAP, but low volume is the risk.",
            "test/model",
        )

    monkeypatch.setattr(web_main, "_generate_ticker_comment_text", generate_comment)
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "sk-or-comment-test")
    request_scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/comments/ONE",
        "headers": [
            (b"origin", APP_ORIGIN.encode()),
            (b"idempotency-key", b"comment-request-0001"),
        ],
        "client": ("127.0.0.78", 4780),
    }
    comment_request = Request(request_scope)
    result = asyncio.run(create_ticker_comment("ONE", comment_request, raw_session))
    replay = asyncio.run(create_ticker_comment("ONE", Request(request_scope), raw_session))
    payload = json.loads(result.body)
    replay_payload = json.loads(replay.body)

    assert result.status_code == 201
    assert replay.status_code == 200
    assert replay_payload == payload
    assert generation_calls == 1
    assert payload["count"] == 1
    assert payload["comment"]["body"] == "My read is above VWAP, but low volume is the risk."
    assert payload["comment"]["ai_generated"] is True
    assert payload["comment"]["generation_model"] == "test/model"
    assert payload["balance"] == 90
    assert payload["comment"]["avatar"]["name"] == payload["comment"]["alias"]
    assert payload["comment"]["avatar"]["ability_id"] in {
        ability["id"] for ability in COMMENT_AVATAR_ABILITIES
    }
    assert payload["comment"]["avatar"]["level"] == 1
    assert "seed" not in payload["comment"]["avatar"]
    assert payload["comment"]["alias"] not in {"chartreader", "Chart Reader"}
    assert "username" not in payload["comment"]
    assert "display_name" not in payload["comment"]
    public_comment = {**payload["comment"], "is_owner": False}
    assert comments_for_ticker("ONE") == [public_comment]
    assert alpha_comments_data() == [{**public_comment, "ticker": "ONE"}]
    assert "radar_case" not in payload
    with connection() as database:
        request_row = database.execute(
            "SELECT status,comment_id FROM comment_generation_requests"
        ).fetchone()
        transaction_count = database.execute(
            "SELECT COUNT(*) FROM flash_transactions WHERE kind='comment_generation'"
        ).fetchone()[0]
    assert request_row["status"] == "completed"
    assert request_row["comment_id"] == payload["comment"]["id"]
    assert transaction_count == 1


def test_ticker_comment_generator_accepts_plain_text_from_openrouter(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        captured["body"] = json.loads(request.data)
        return io.BytesIO(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "My read is constructive, but thin volume is the risk."
                                )
                            }
                        }
                    ],
                    "model": "z-ai/glm-5.3",
                }
            ).encode()
        )

    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "sk-or-comment-test")
    monkeypatch.setattr(
        web_main,
        "ticker_detail_data",
        lambda ticker: {
            "ticker": ticker,
            "company": "One Corp",
            "current": {
                "price": 1.25,
                "change_pct": 4.0,
                "signals": ["Price is above VWAP"],
                "risks": ["Volume is thin"],
            },
            "evidence_gate": {},
            "events": [],
        },
    )
    monkeypatch.setattr(web_main.urllib.request, "urlopen", fake_urlopen)

    comment, model = web_main._generate_ticker_comment_text(
        "ONE",
        avatar_ability_id="risk_sentinel",
    )

    assert comment == "My read is constructive, but thin volume is the risk."
    assert model == "z-ai/glm-5.3"
    assert captured["body"]["models"] == list(web_main.OPENROUTER_COMMENT_MODELS)
    assert len(captured["body"]["models"]) <= web_main.OPENROUTER_COMMENT_MODEL_LIMIT
    assert "model" not in captured["body"]
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["provider"] == {
        "allow_fallbacks": True,
        "require_parameters": True,
        "zdr": True,
    }
    assert captured["body"]["max_tokens"] == web_main.OPENROUTER_COMMENT_OUTPUT_TOKENS
    assert captured["body"]["max_tokens"] >= 1_200
    assert "user" not in captured["body"]
    assert "Risk Sentinel" in captured["body"]["messages"][0]["content"]
    assert "public name" not in captured["body"]["messages"][0]["content"]


def test_failed_ticker_comment_request_refunds_once_and_replays_the_error(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "failed-comment.db")
    init_db()
    timestamp = datetime.now(UTC)
    insert_filing("failed-comment-one", "ONE", 1.25, 70, timestamp.isoformat(), "P")
    raw_session = "failed-comment-session"
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("failed-comment-user", "failed", "Failed", "active", timestamp.isoformat()),
        )
        database.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (
                web_main.token_hash(raw_session),
                "failed-comment-user",
                timestamp.isoformat(),
                (timestamp + timedelta(days=1)).isoformat(),
            ),
        )
    claim_daily_flash("failed-comment-user")
    generation_calls = 0

    def fail_generation(ticker: str, *, avatar_ability_id: str) -> tuple[str, str]:
        nonlocal generation_calls
        generation_calls += 1
        raise HTTPException(502, "AI comment generation failed. Your Flash was returned.")

    monkeypatch.setattr(web_main, "_generate_ticker_comment_text", fail_generation)
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "sk-or-comment-test")
    request_scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/comments/ONE",
        "headers": [
            (b"origin", APP_ORIGIN.encode()),
            (b"idempotency-key", b"failed-comment-request-0001"),
        ],
        "client": ("127.0.0.79", 4781),
    }

    for _attempt in range(2):
        with pytest.raises(HTTPException) as error:
            asyncio.run(create_ticker_comment("ONE", Request(request_scope), raw_session))
        assert error.value.status_code == 502
        assert error.value.detail == "AI comment generation failed. Your Flash was returned."

    assert generation_calls == 1
    assert wallet_for_user("failed-comment-user")["balance"] == 100
    with connection() as database:
        request_row = database.execute(
            "SELECT status,error_status FROM comment_generation_requests"
        ).fetchone()
        transactions = database.execute(
            "SELECT kind,amount FROM flash_transactions ORDER BY created_at,id"
        ).fetchall()
        comment_count = database.execute("SELECT COUNT(*) FROM ticker_comments").fetchone()[0]
    assert request_row["status"] == "failed"
    assert request_row["error_status"] == 502
    assert {(row["kind"], row["amount"]) for row in transactions} >= {
        ("comment_generation", -10),
        ("comment_refund", 10),
    }
    assert comment_count == 0


def test_ticker_comment_generator_retries_invalid_content_with_other_models(
    monkeypatch: MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []
    responses = [
        {
            "choices": [{"message": {"content": {"wrong": "shape"}}}],
            "model": "contender/one",
        },
        {
            "choices": [
                {"message": {"content": {"comment": "My view is mixed; cash is the risk."}}}
            ],
            "model": "contender/two",
        },
    ]

    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        assert timeout == 30
        requests.append(json.loads(request.data))
        assert request.get_header("X-openrouter-metadata") == "enabled"
        return io.BytesIO(json.dumps(responses.pop(0)).encode())

    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "sk-or-comment-test")
    monkeypatch.setattr(
        web_main,
        "OPENROUTER_COMMENT_MODELS",
        ("contender/one", "contender/two", "contender/three"),
    )
    monkeypatch.setattr(
        web_main,
        "ticker_detail_data",
        lambda ticker: {
            "ticker": ticker,
            "company": "One Corp",
            "current": {},
            "evidence_gate": {},
            "events": [],
        },
    )
    monkeypatch.setattr(web_main.urllib.request, "urlopen", fake_urlopen)

    comment, model = web_main._generate_ticker_comment_text("ONE")

    assert comment == "My view is mixed; cash is the risk."
    assert model == "contender/two"
    assert requests[0]["models"] == ["contender/one", "contender/two", "contender/three"]
    assert requests[1]["models"] == ["contender/two", "contender/three"]


def test_openrouter_route_diagnostics_exclude_prompts_and_raw_errors() -> None:
    payload = {
        "error": {
            "code": 502,
            "message": "private prompt text",
            "metadata": {
                "raw": "provider secret",
                "attempts": [
                    {
                        "provider_name": "Provider A",
                        "model": "contender/one",
                        "status": 503,
                        "raw": "upstream body",
                    }
                ],
            },
        }
    }

    diagnostics = web_main._openrouter_route_diagnostics(payload)

    assert diagnostics == {
        "error_code": 502,
        "attempts": [
            {"provider_name": "Provider A", "model": "contender/one", "status": 503}
        ],
    }
    assert "private prompt text" not in json.dumps(diagnostics)
    assert "provider secret" not in json.dumps(diagnostics)


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
    insert_scan_run("activity-run", captured_at.isoformat(), 2)
    insert_scored_snapshot(
        "activity-one-snapshot", "activity-run", "ONE", 70, 1, captured_at.isoformat()
    )
    insert_scored_snapshot(
        "activity-two-snapshot", "activity-run", "TWO", 60, 2, captured_at.isoformat()
    )
    result = radar_data()

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
    _approve_test_source("test_media", "social")

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


def test_commissioned_report_stays_private_without_storing_the_openrouter_key(
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
    claim_daily_flash("commissioner")
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
                "forecast": _test_flash_forecast(),
            },
            "z-ai/glm-5.3",
            {"total_tokens": 321},
        ),
    )

    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "sk-or-server-test-key")
    report = _commission_research("commissioner", "ONE")

    assert report["headline"] == "ONE evidence report"
    assert report["thesis"] == "A source-bound summary."
    assert report["research_mode"] == "one_shot_system_context"
    assert report["actor"]["id"] == "kol-flash"
    assert report["actor"]["display_name"] == "Flash"
    assert report["actor"]["model"] == "z-ai/glm-5.3"
    assert report["actor"]["ladder_position"] == 1
    assert report["actor"]["ladder_size"] == 4
    assert get_commission(report["public_id"])["summary"] == "A source-bound summary."
    assert wallet_for_user("commissioner")["balance"] == 0
    assert "commissions" not in alpha_board_data()
    with connection() as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(research_commissions)")}
        stored = database.execute("SELECT * FROM research_commissions").fetchone()
    assert "openrouter_key" not in columns
    assert "sk-or-server-test-key" not in " ".join(str(value) for value in stored)

    same_report, created = web_main._create_research_commission("commissioner", "ONE")
    assert created is False
    assert same_report["public_id"] == report["public_id"]
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM research_commissions").fetchone()[0] == 1


def test_browser_commission_uses_the_server_openrouter_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "verified-commission.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("verified-one", "ONE", 1.25, 90, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("verified-user", "member_verified", "Member", "active", captured_at),
        )
    claim_daily_flash("verified-user")
    captured: dict[str, str] = {}

    def fake_report(key: str, context: dict[str, Any], user_id: str, **kwargs: Any) -> Any:
        captured["key"] = key
        return (
            {
                "headline": "ONE checked report",
                "thesis": "The stored evidence is mixed.",
                "summary": "Flash reviewed the stored evidence.",
                "catalysts": ["Verified catalyst"],
                "risks": ["Verified risk"],
                "watch": ["New filing"],
                "unknowns": ["Financing terms"],
                "sources": context["sources"],
                "forecast": _test_flash_forecast(),
            },
            "z-ai/glm-5.3",
            {},
        )

    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "sk-or-server-side-test-key")
    monkeypatch.setattr(web_main, "_generate_openrouter_report", fake_report)

    report = _commission_research("verified-user", "ONE")

    assert report["status"] == "complete"
    assert report["research_mode"] == "one_shot_system_context"
    assert report["usage"]["research_mode"] == "one_shot_system_context"
    assert report["headline"] == "ONE checked report"
    assert captured["key"] == "sk-or-server-side-test-key"


def test_verified_pipeline_freezes_every_stage(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "research-stages.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("stage-one", "ONE", 1.25, 70, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("stage-user", "member_stage", "Member", "active", captured_at),
        )
    claim_daily_flash("stage-user")
    commission, created = web_main._create_research_commission("stage-user", "ONE")
    assert created is True

    def fake_stage(
        stage: str,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        *,
        model: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del instructions, payload, schema
        metadata = {
            "provider": "openai",
            "model": model,
            "provider_request_id": f"request-{stage}",
            "usage": {"total_tokens": 12},
        }
        if stage in {
            "catalyst_researcher",
            "financing_skeptic",
            "market_liquidity_checker",
        }:
            return {"findings": [], "unknowns": []}, metadata
        if stage == "independent_critic":
            return (
                {
                    "supported_statements": [],
                    "rejected_statements": [],
                    "conflicts": [],
                    "required_caveats": [],
                    "verdict": "unchanged",
                },
                metadata,
            )
        return (
            {
                "headline": "ONE remains a watch",
                "thesis": "No verified change yet.",
                "summary": "The stored evidence does not support a stronger view.",
                "case_effect": "unchanged",
                "market_view": "neutral",
                "confidence": 0.4,
                "catalysts": [],
                "risks": [],
                "watch": [],
                "unknowns": [],
                "sources": [],
            },
            metadata,
        )

    monkeypatch.setattr(web_main, "_generate_openai_stage", fake_stage)
    report, model, usage = web_main._generate_verified_report(
        commission["id"],
        {"ticker": "ONE", "primary_evidence": {}, "context_sections": [], "sources": []},
        actor=web_main.FLASH,
    )

    with connection() as database:
        stages = database.execute(
            "SELECT * FROM research_stage_runs WHERE commission_id=? ORDER BY stage_order",
            (commission["id"],),
        ).fetchall()
    assert report["headline"] == "ONE remains a watch"
    assert model == "z-ai/glm-5.3"
    assert usage["pipeline_version"] == "verified-research-v1"
    assert [row["stage"] for row in stages] == [
        "catalyst_researcher",
        "financing_skeptic",
        "market_liquidity_checker",
        "independent_critic",
        "synthesis",
    ]
    assert all(row["status"] == "complete" for row in stages)
    assert all(row["prompt_version"] == "verified-research-v1" for row in stages)
    assert all(len(row["input_fingerprint"]) == 64 for row in stages)


def test_first_daily_flash_report_locks_other_users_for_one_hour(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "running-commission.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    raw_session = "daily-report-session"
    insert_filing("running-one", "ONE", 1.25, 90, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("runner", "member_runner", "Member", "active", captured_at),
        )
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("other", "member_other", "Other member", "active", captured_at),
        )
        database.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (
                web_main.token_hash(raw_session),
                "runner",
                captured_at,
                (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            ),
        )
    claim_daily_flash("runner")
    claim_daily_flash("other")

    first, first_created = web_main._create_research_commission("runner", "ONE")
    with pytest.raises(HTTPException) as locked:
        web_main._create_research_commission("other", "ONE")

    assert first_created is True
    assert first["status"] == "running"
    assert locked.value.status_code == 423
    assert web_main.latest_commission("runner", "ONE")["status"] == "running"
    assert web_main.latest_commission("other", "ONE") is None
    assert web_main.daily_report_for_ticker("ONE", "runner")["locked"] is False
    assert web_main.daily_report_for_ticker("ONE", "other")["locked"] is True
    with connection() as database:
        rows = database.execute("SELECT * FROM research_commissions").fetchall()
        requests = database.execute(
            "SELECT user_id,created_new FROM flash_report_requests ORDER BY created_at,id"
        ).fetchall()
    assert len(rows) == 1
    assert wallet_for_user("runner")["balance"] == 0
    assert wallet_for_user("other")["balance"] == 100
    assert requests == []
    assert "openrouter" not in " ".join(rows[0].keys()).lower()

    expired_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    completed_at = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            """
            UPDATE research_commissions
            SET status='complete',headline='ONE daily alpha',summary='Daily evidence.',
                completed_at=?,updated_at=?,exclusive_until=?
            WHERE id=?
            """,
            (completed_at, completed_at, expired_at, first["id"]),
        )

    release_reports = web_main._release_expired_daily_reports
    monkeypatch.setattr(
        web_main,
        "_release_expired_daily_reports",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("daily report reads must not run a release update")
        ),
    )
    shared = web_main.daily_report_for_ticker("ONE", "other")
    monkeypatch.setattr(web_main, "_release_expired_daily_reports", release_reports)
    same_report, created = web_main._create_research_commission("other", "ONE")

    assert shared is not None
    assert shared["visibility"] == "public"
    assert shared["locked"] is False
    assert created is False
    assert same_report["public_id"] == first["public_id"]
    assert wallet_for_user("other")["balance"] == 100

    publish_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/research/{first['public_id']}/publish",
            "headers": [(b"origin", APP_ORIGIN.encode())],
            "client": ("127.0.0.90", 4900),
        }
    )
    late_publish = json.loads(
        web_main.publish_research_report_api(
            first["public_id"], publish_request, raw_session
        ).body
    )
    assert late_publish["published"] is False
    assert late_publish["reward"] == 0
    assert late_publish["balance"] == 0


def test_ticker_and_sports_share_one_flash_report_workflow() -> None:
    root = Path(__file__).parents[1]
    ticker_template = (root / "web/templates/ticker.html").read_text()
    sports_template = (root / "web/templates/sports_game.html").read_text()
    action_template = (root / "web/templates/_flash_report_action.html").read_text()
    report_script = (root / "web/static/flash-report.js").read_text()
    ticker_script = (root / "web/static/ticker-detail.js").read_text()

    assert "flash_report_action(flash_report)" in ticker_template
    assert "flash_report_action(flash_report, 'sports')" in sports_template
    assert "/static/flash-report.js" in ticker_template
    assert "/static/flash-report.js" in sports_template
    assert "Generate report" in report_script
    assert "Report couldn't be generated" in report_script
    assert "data-start-url" in action_template
    assert "pollFlash" not in ticker_script + sports_template
    assert "Sending the report to the queue" not in report_script + sports_template
    assert "OpenRouter" not in action_template + report_script
    assert "X-OpenRouter-Key" not in action_template + report_script


def test_flash_model_label_is_shown_on_research_and_pulse() -> None:
    root = Path(__file__).parents[1]
    report = (root / "web/templates/research_report.html").read_text()
    pulse = (root / "web/templates/pulse.html").read_text()
    ticker_row = (root / "web/static/ticker-row.js").read_text()

    assert "{{ report.actor.display_name }}" in report
    assert "{{ report.actor.model }}" in report
    assert "flash_record.model_label" in pulse
    assert "call.inference_model_label" not in ticker_row


def test_flash_record_page_and_api_show_the_fixed_contract(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "flash-record-page.db")
    monkeypatch.setattr(web_main, "rate_limit_allowed", lambda *args, **kwargs: True)
    init_db()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/flash/record",
            "headers": [],
            "client": ("127.0.0.1", 4300),
        }
    )
    request.state.csp_nonce = "test"

    page = web_main.flash_record_page(request, None)
    payload = web_main.api_flash_record(request)

    assert page.status_code == 200
    assert "Forecast record" in page.body.decode()
    assert "Frozen calls. Later prices. No rewrites." in page.body.decode()
    assert payload["contract"]["id"] == "flash-next-session-v1"
    assert payload["current_version"]["headline_rate_visible"] is False


def test_legacy_commission_request_uses_flash_model_with_a_minimal_prompt(
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
        "citations": [],
        "forecast": _test_flash_forecast(),
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
    assert body["plugins"] == [{"id": "response-healing"}]
    assert body["provider"] == {"require_parameters": True, "zdr": True}
    assert body["reasoning_effort"] == "high"
    assert "temperature" not in body
    assert "tools" not in body
    assert "untrusted evidence" in body["messages"][0]["content"]
    assert len(body["messages"][0]["content"].split()) <= 75
    assert "evaluation_contract" in request_payload
    assert request_payload["output"]["forecast"]["direction"] == "up, down, or no_call"
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
                                        "forecast": _test_flash_forecast(),
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


def test_flash_keeps_only_citations_from_the_frozen_context(
    monkeypatch: MonkeyPatch,
) -> None:
    allowed = "https://www.sec.gov/Archives/edgar/data/1/report.htm"
    invented = "https://invented.example/report"
    generated = {
        "headline": "ONE evidence report",
        "thesis": "The filing is the only verified source.",
        "summary": "The filing is the only verified source.",
        "company_profile": {"source_urls": [invented]},
        "people": [],
        "filings": [],
        "catalysts": [],
        "risks": [],
        "watch": [],
        "unknowns": [],
        "sources": [allowed, invented],
        "citations": [
            {"claim": "Verified claim", "source_urls": [allowed, invented]},
            {"claim": "Invented claim", "source_urls": [invented]},
        ],
        "forecast": _test_flash_forecast(),
    }

    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        return io.BytesIO(
            json.dumps(
                {
                    "choices": [{"message": {"content": json.dumps(generated)}}],
                    "model": "z-ai/glm-5.3",
                }
            ).encode()
        )

    monkeypatch.setattr(web_main.urllib.request, "urlopen", fake_urlopen)
    report, _, _ = web_main._generate_openrouter_report(
        "server-key",
        {"ticker": "ONE", "sources": [allowed]},
        "member-one",
    )

    assert report["sources"] == [allowed]
    assert report["company_profile"]["source_urls"] == []
    assert report["citations"] == [
        {"claim": "Verified claim", "source_urls": [allowed]}
    ]
    assert invented not in json.dumps(report)


@pytest.mark.parametrize("wrapper", ["answer", "output"])
@pytest.mark.parametrize("as_text", [False, True])
def test_commission_unwraps_glm_report_envelope(
    monkeypatch: MonkeyPatch,
    wrapper: str,
    as_text: bool,
) -> None:
    generated = {
        "headline": "EU evidence report",
        "thesis": "The stored evidence is mixed.",
        "summary": "Watch for confirmation.",
        "company_profile": {"source_urls": []},
        "people": [],
        "filings": [],
        "catalysts": [],
        "risks": [],
        "watch": [],
        "unknowns": [],
        "sources": [],
        "citations": [],
        "forecast": _test_flash_forecast(),
    }
    wrapped_report: Any = json.dumps(generated) if as_text else generated

    def fake_urlopen(request: Any, timeout: int) -> io.BytesIO:
        return io.BytesIO(
            json.dumps(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps({wrapper: wrapped_report})
                            },
                        }
                    ],
                    "model": "z-ai/glm-5.3",
                }
            ).encode()
        )

    monkeypatch.setattr(web_main.urllib.request, "urlopen", fake_urlopen)
    report, model, usage = web_main._generate_openrouter_report(
        "sk-or-test-key-long-enough", {"ticker": "EU"}, "member-eu"
    )

    assert report == generated
    assert model == "z-ai/glm-5.3"
    assert usage["generation"]["normalized_fields"] == []


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
    claim_daily_flash("failed-user")

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

    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "sk-or-server-test-key")
    monkeypatch.setattr(web_main, "_generate_openrouter_report", fail_report)
    with pytest.raises(web_main.ReportGenerationFailure):
        _commission_research("failed-user", "EU")

    failed = web_main.latest_commission("failed-user", "EU")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["usage"]["failure"]["failure_kind"] == "invalid_json"
    assert wallet_for_user("failed-user")["balance"] == 100
    payload = web_main._commission_api_payload(failed, "failed-user")
    assert payload["retryable"] is True
    assert payload["error"] == "Report couldn't be generated. No Flash was charged."
    assert payload["balance"] == 100
    assert payload["refunded"] == 100
    assert "malformed" not in json.dumps(payload)
    assert "OPENROUTER_API_KEY" not in json.dumps(failed)
    retry, created = web_main._create_research_commission("failed-user", "EU")
    assert created is True
    assert retry["status"] == "running"
    assert wallet_for_user("failed-user")["balance"] == 0


def test_flash_report_circuit_breaker_stops_promising_failed_reports(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "flash-circuit.db")
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "server-test-key")
    init_db()
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("circuit-user", "circuit_user", "Circuit User", "active", timestamp),
        )
        for index in range(web_main.FLASH_REPORT_FAILURE_STREAK_LIMIT):
            database.execute(
                """
                INSERT INTO research_commissions(
                    id,public_id,user_id,ticker,evidence_key,status,requested_model,
                    actor_id,created_at,updated_at,report_day,error
                ) VALUES(?,?,?,?,?,'failed',?,?,?,?,?,?)
                """,
                (
                    f"failed-{index}",
                    f"failed-public-{index}",
                    "circuit-user",
                    "FAIL",
                    f"evidence-{index}",
                    web_main.FLASH.model,
                    web_main.FLASH.id,
                    timestamp,
                    timestamp,
                    datetime.now(UTC).date().isoformat(),
                    "Internal provider detail",
                ),
            )

    assert web_main._flash_provider_ready() is False
    assert web_main._flash_daily_capacity_available() is True
    report_action = web_main._flash_report_action(
        user_id="circuit-user",
        latest_report=None,
        latest_attempt=web_main.latest_commission("circuit-user", "FAIL"),
        start_url="/api/research/FAIL",
        login_url="/login?next=/t/FAIL",
    )
    assert report_action["state"] == "unavailable"
    assert report_action["label"] == "Report unavailable"
    assert report_action["message"] == "Report couldn't be generated. No Flash was charged."
    assert "provider" not in json.dumps(report_action).lower()


def test_flash_queue_failure_refunds_before_the_response(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "flash-queue.db")
    init_db()
    timestamp = datetime.now(UTC).isoformat()
    insert_filing("queue-one", "ONE", 1.25, 90, timestamp, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("queue-user", "queue_user", "Queue User", "active", timestamp),
        )
    claim_daily_flash("queue-user")
    report, created = web_main._create_research_commission("queue-user", "ONE")
    assert created is True
    assert wallet_for_user("queue-user")["balance"] == 0
    monkeypatch.setattr(web_main, "redis_configured", lambda: True)

    def fail_enqueue(_report_id: str) -> None:
        raise RuntimeError("private queue detail")

    monkeypatch.setattr(web_main, "enqueue_research_job", fail_enqueue)
    failed = asyncio.run(web_main._enqueue_created_research_report(report, "queue-user"))
    payload = web_main._commission_api_payload(failed, "queue-user")

    assert payload["status"] == "failed"
    assert payload["balance"] == 100
    assert payload["message"] == "Report couldn't be generated. No Flash was charged."
    assert "queue" not in json.dumps(payload).lower()


def test_owner_can_publish_report_once_and_earn_flash(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "share-report.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    raw_session = "share-session"
    insert_filing("share-one", "ONE", 1.25, 90, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("sharer", "member_sharer", "Member", "active", captured_at),
        )
        database.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (
                web_main.token_hash(raw_session),
                "sharer",
                captured_at,
                (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            ),
        )
    claim_daily_flash("sharer")
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
                "forecast": _test_flash_forecast(),
            },
            "z-ai/glm-5.3",
            {},
        ),
    )
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "sk-or-share-test-key")
    report = _commission_research("sharer", "ONE")
    public_request = Request(
        {"type": "http", "method": "GET", "path": "/research", "headers": []}
    )
    public_request.state.csp_nonce = "test"
    with pytest.raises(HTTPException) as private_error:
        web_main.research_report_page(report["public_id"], public_request, None)
    assert private_error.value.status_code == 404

    publish_request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/research/{report['public_id']}/publish",
            "headers": [(b"origin", APP_ORIGIN.encode())],
            "client": ("127.0.0.88", 4880),
        }
    )
    first_publish = json.loads(
        web_main.publish_research_report_api(
            report["public_id"], publish_request, raw_session
        ).body
    )
    second_publish = json.loads(
        web_main.publish_research_report_api(
            report["public_id"], publish_request, raw_session
        ).body
    )

    assert first_publish["published"] is True
    assert first_publish["reward"] == 50
    assert first_publish["balance"] == 50
    assert second_publish["published"] is False
    assert second_publish["reward"] == 0
    assert second_publish["balance"] == 50
    public_page = web_main.research_report_page(report["public_id"], public_request, None)
    assert public_page.status_code == 200
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
    assert "<strong>Flash <span class=\"flash-model-label\">z-ai/glm-5.3</span>" in html
    assert "#1 of 4" in html
    assert f"/research/{report['public_id']}/card.png" in html
    assert "sk-or-share-test-key" not in html
