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
    }


def _rendered_pulse(monkeypatch, payload: dict[str, Any]) -> str:
    monkeypatch.setattr(web_main, "pulse_data", lambda **_kwargs: payload)
    html = web_main.home(_request(), None).body.decode()
    ticker_script = (ROOT / "web/static/ticker-row.js").read_text()
    kol_styles = (ROOT / "web/static/kol.css").read_text()
    html = html.replace("<head>", '<head><base href="http://app.test/">')
    html = html.replace("</head>", f"<style>{kol_styles}</style></head>")
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
    assert errors == []


def test_refresh_executes_missing_markers_and_clears_stale_alerts(page: Page, monkeypatch) -> None:
    initial = _pulse(_row("AAA"))
    newer = _pulse(_row("BBB"), _row("AAA"))
    settled = _pulse(_row("AAA"))
    html = _rendered_pulse(monkeypatch, initial)
    errors = _load(page, html, [newer, settled])

    page.evaluate("pollForUpdates()")
    assert page.locator("#pulseRefresh").is_visible()
    assert page.locator("#pulseRefresh").text_content() == "1 new ticker"

    page.evaluate("pollForUpdates()")
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

    page.evaluate("pollForUpdates()")
    assert page.locator("#kolScoreStrip").is_visible()
    assert "12 hits · 8 misses · 50% hit" in page.locator("[data-kol-stats]").text_content()
    assert page.locator("[data-kol-pnl]").text_content() == "20 settled"
    page.locator("#pulseRefresh").click()
    assert page.locator('[data-ticker-row="BBB"]').count() == 1
    assert page.locator('[data-ticker-row="AAA"]').count() == 1
    assert page.locator("#pulseRefresh").is_hidden()
    assert errors == []
