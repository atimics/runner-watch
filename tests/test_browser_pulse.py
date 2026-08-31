from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route
from starlette.requests import Request

from runner_web import main as web_main

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"host", b"runners.rati.chat")],
            "scheme": "https",
            "server": ("runners.rati.chat", 443),
            "client": ("127.0.0.1", 1234),
            "query_string": b"",
        }
    )
    request.state.csp_nonce = "browser-test"
    return request


def _row(ticker: str, entered_at: str | None = None) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "company": f"{ticker} Company",
        "price": 1.25,
        "change_pct": 4.2,
        "pulse_label": "Test event",
        "entered_at": entered_at,
    }


def _pulse(
    *rows: dict[str, Any],
    flash_record: dict[str, Any] | None = None,
    has_more: bool = False,
) -> dict[str, Any]:
    return {
        "rows": list(rows),
        "flash_record": flash_record,
        "has_more": has_more,
        "next_offset": len(rows),
        "updated_at": "2026-08-27T18:00:00+00:00",
    }


def _rendered_pulse(monkeypatch, payload: dict[str, Any]) -> str:
    monkeypatch.setattr(web_main, "pulse_data", lambda **_kwargs: payload)
    monkeypatch.setattr(
        web_main,
        "market_reports_overview",
        lambda **_kwargs: {
            "featured": None,
            "schedule": {
                "next_label": "Pre-market briefing",
                "schedule_note": "Weekdays · 9:00 ET and 4:15 ET",
            },
        },
    )
    html = web_main.home(_request(), None).body.decode()
    live_list_script = (ROOT / "web/static/live-list.js").read_text()
    ticker_script = (ROOT / "web/static/ticker-row.js").read_text()
    html = html.replace("<head>", '<head><base href="http://app.test/">')
    html = re.sub(
        r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
        lambda match: f"<style>{(ROOT / 'web/static' / match.group(1)).read_text()}</style>",
        html,
    )
    html = re.sub(
        r'<script src="/static/live-list\.js[^\"]*"[^>]*></script>',
        lambda _match: f"<script>{live_list_script}</script>",
        html,
    )
    html = re.sub(
        r'<script src="/static/ticker-row\.js[^\"]*"[^>]*></script>',
        lambda _match: f"<script>{ticker_script}</script>",
        html,
    )
    return re.sub(r'<script src="/static/[^\"]+"[^>]*></script>', "", html)


def _load(
    page: Page,
    html: str,
    poll_responses: list[dict[str, Any]],
) -> list[BaseException]:
    errors: list[BaseException] = []
    page.on("pageerror", lambda error: errors.append(error))

    def pulse_response(route: Route) -> None:
        payload = poll_responses.pop(0) if poll_responses else _pulse()
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/pulse?*", pulse_response)
    page.route(
        "**/api/pulse/charts",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"charts":{},"annotations":{}}',
        ),
    )
    page.set_content(html, wait_until="domcontentloaded")
    return errors


def test_empty_flash_record_has_no_layout_gap(page: Page, monkeypatch) -> None:
    html = _rendered_pulse(monkeypatch, _pulse(_row("AAA")))
    errors = _load(page, html, [])

    scorecard = page.locator("#kolScoreStrip")
    assert scorecard.is_hidden()
    assert scorecard.evaluate("element => element.getBoundingClientRect().height") == 0
    assert "Session +4.2%" in page.locator('[data-ticker-row="AAA"] .quote').text_content()
    assert page.locator(".market-turn-strip").is_visible()
    assert page.locator(".market-turn-strip strong").text_content() == "Pre-market briefing"
    assert page.evaluate(
        "TickerRow.ago(new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString())"
    ) == "3h ago"
    assert errors == []


@pytest.mark.parametrize("width", [390, 1280])
def test_pulse_header_is_one_compact_stack(page: Page, monkeypatch, width: int) -> None:
    record = {
        "label": "Flash 2026.09",
        "model_label": "GLM 5.3",
        "hits": 1,
        "misses": 0,
        "settled": 1,
        "hit_rate": 1.0,
        "headline_rate_visible": False,
    }
    page.set_viewport_size({"width": width, "height": 800})
    html = _rendered_pulse(monkeypatch, _pulse(_row("AAA"), flash_record=record))
    errors = _load(page, html, [])

    search = page.locator("#pulseSearch")
    scorecard = page.locator("#kolScoreStrip")
    assert search.is_visible()
    assert search.evaluate("element => element.closest('.account-strip') !== null") is True
    assert page.locator(".pulse-list-column > .pulse-search").count() == 0
    assert page.locator(".pulse-list-column > .session-clock").count() == 0
    assert page.locator(".screen-head .head-meta").count() == 0
    assert page.locator(".pulse-context").is_visible()
    assert "Ranked by Pulse score" in page.locator(".pulse-context").text_content()
    assert "7-day sparklines share one % scale" in page.locator(".pulse-context").text_content()
    assert scorecard.is_visible()
    assert scorecard.evaluate("element => element.getBoundingClientRect().height") <= 34
    assert page.evaluate(
        "document.querySelector('.account-ticker-search').getBoundingClientRect().left === "
        "document.querySelector('.account-strip').getBoundingClientRect().left"
    ) is True
    assert errors == []


def test_pulse_renders_the_forward_thesis_as_a_tiny_separate_cue(
    page: Page, monkeypatch
) -> None:
    row = {
        **_row("AAA"),
        "directional_thesis": {
            "direction": "down",
            "label": "Downside pressure",
            "arrow": "↓",
            "horizon": "60m",
        },
    }
    html = _rendered_pulse(monkeypatch, _pulse(row))
    errors = _load(page, html, [])

    cue = page.locator('[data-ticker-row="AAA"] .ticker-thesis')
    assert cue.text_content() == "↓DOWNSIDE PRESSURE · 60M"
    assert cue.get_attribute("class") == "ticker-thesis ticker-thesis-down"
    assert cue.evaluate("element => element.closest('.catalyst') !== null") is True
    assert "Directional thesis: Downside pressure, 60m" in page.locator(
        '[data-ticker-row="AAA"]'
    ).get_attribute("aria-label")
    assert errors == []


def test_pulse_labels_an_unavailable_market_model_as_learning(page: Page, monkeypatch) -> None:
    html = _rendered_pulse(monkeypatch, _pulse({**_row("AAA"), "source": "market"}))
    errors = _load(page, html, [])

    assert page.locator('[data-ticker-row="AAA"] .ticker-thesis-learning').count() == 0
    assert "Directional thesis: model learning" in page.locator(
        '[data-ticker-row="AAA"]'
    ).get_attribute("aria-label")
    assert errors == []


def test_scored_pulse_rows_expose_comparable_rank_and_signal_metrics(
    page: Page, monkeypatch
) -> None:
    row = {
        **_row("AAA"),
        "section": "scored",
        "custom_rank": 1,
        "score": 72.4,
        "setup_score": 61.2,
        "relative_volume": 3.18,
        "momentum_15m_pct": -2.4,
    }
    html = _rendered_pulse(monkeypatch, _pulse(row))
    errors = _load(page, html, [])

    comparison = page.locator('[data-ticker-row="AAA"] .ticker-comparison')
    assert comparison.text_content() == "RANK#1PULSE72SETUP61RVOL3.2×15M-2.4%"
    assert comparison.locator("b.down").text_content() == "-2.4%"
    assert comparison.evaluate(
        "element => getComputedStyle(element.closest('.ticker-row')).minHeight"
    ) == "94px"
    assert errors == []


def test_refresh_executes_missing_markers_and_clears_stale_alerts(page: Page, monkeypatch) -> None:
    initial = _pulse(_row("AAA"))
    newer = _pulse(_row("BBB"), _row("AAA"))
    settled = _pulse(_row("AAA"))
    html = _rendered_pulse(monkeypatch, initial)
    errors = _load(page, html, [newer, settled])

    page.evaluate("window.pulseLive.poll()")
    assert page.locator("#pulseRefresh").is_visible()
    assert page.locator("#pulseRefresh").text_content() == "1 new ticker"

    page.evaluate("window.pulseLive.poll()")
    assert page.locator("#pulseRefresh").is_hidden()
    assert errors == []


def test_refresh_updates_flash_record_and_merges_new_ticker(page: Page, monkeypatch) -> None:
    initial = _pulse(_row("AAA", "2026-08-26T18:00:00+00:00"))
    record = {
        "label": "Flash 2026.09",
        "model_label": "GLM 5.3",
        "hits": 12,
        "misses": 8,
        "settled": 20,
        "hit_rate": 0.5,
        "headline_rate_visible": True,
    }
    newer = _pulse(
        _row("BBB", "2026-08-26T18:05:00+00:00"),
        _row("AAA", "2026-08-26T18:00:00+00:00"),
        flash_record=record,
    )
    html = _rendered_pulse(monkeypatch, initial)
    errors = _load(page, html, [newer])

    page.evaluate("window.pulseLive.poll()")
    assert page.locator("#kolScoreStrip").is_visible()
    assert "12 hits · 8 misses · 50% hit" in page.locator("[data-kol-stats]").text_content()
    assert page.locator("[data-kol-pnl]").text_content() == "View record ›"
    page.locator("#pulseRefresh").click()
    assert page.locator('[data-ticker-row="BBB"]').count() == 1
    assert page.locator('[data-ticker-row="AAA"]').count() == 1
    assert page.locator("#pulseRefresh").is_hidden()
    assert errors == []
