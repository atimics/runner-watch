"""Exercise the saved replay on the actual coin details template."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from starlette.requests import Request

from runner_web import main
from tests.test_browser_memecoins import _detail
from tests.test_memecoin_replay import COIN, payload

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parents[1]


def open_replay(page: Page, *, width: int = 390, launch: bool = True) -> dict:
    page.set_viewport_size({"width": width, "height": 900})
    data = payload()
    if not launch:
        from runner_web.memecoin_replay import build_replay
        from tests.test_memecoin_replay import receipts, transaction

        data = build_replay(COIN, receipts([transaction("buy", 2)]), {})
    record = {
        "status": "ready",
        "id": data["id"],
        "payload": data,
        "gif_url": "/saved.gif",
        "evidence_url": "/saved.json",
    }
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/memecoins/coin/" + COIN["id"],
            "headers": [(b"host", b"app.test")],
            "scheme": "http",
            "server": ("app.test", 80),
            "query_string": b"",
        }
    )
    request.state.csp_nonce = "browser-test"
    detail = _detail(coin={**_detail()["coin"], **COIN})
    html = main.templates.TemplateResponse(
        request,
        "simple_coin_detail.html",
        main.page_context(
            request,
            None,
            resolved_user=None,
            detail=detail,
            active_call=None,
        ),
    ).body.decode()
    html = re.sub(
        r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
        lambda match: "<style>" + (ROOT / "web/static" / match[1]).read_text() + "</style>",
        html,
    )
    html = re.sub(
        r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>',
        lambda match: "<script>" + (ROOT / "web/static" / match[1]).read_text() + "</script>",
        html,
    )
    page.route(
        "http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html")
    )
    page.route("**/api/memecoins/*/replay*", lambda route: route.fulfill(json=record))
    page.route(
        "**/api/screens/**",
        lambda route: route.fulfill(json=main.simple_market_detail("memecoins", detail)),
    )
    page.goto("http://app.test/memecoins/coin/" + COIN["id"])
    expect(page.locator("[data-replay-content]")).to_be_visible()
    return data


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_coin_map_uses_stock_layout_and_ticker_center(page: Page, width: int):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    data = open_replay(page, width=width)
    expect(page.locator("[data-replay-graph]")).to_have_attribute("data-phase", "settled")
    expect(page.locator(".map-score-center")).to_contain_text(COIN["symbol"])
    expect(page.locator(".map-canvas")).to_be_visible()
    expect(page.locator("[data-replay-events] button")).to_have_count(
        len({e["event_id"] for e in data["frames"][-1]["edges"]})
    )
    expect(page.get_by_role("region", name="Token evidence")).to_be_visible()
    expect(page.locator("[data-replay-gif], .replay-controls")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_keyboard_bubble_opens_evidence_and_returns_to_score(page: Page):
    open_replay(page, width=1440)
    wallet = page.get_by_role("button", name=re.compile(r"^wallet:"))
    wallet.first.focus()
    wallet.first.press("Enter")
    expect(wallet.first).to_be_focused()
    expect(page.locator("[data-replay-selection] a").first).to_have_attribute(
        "href", re.compile("/api/memecoins/evidence/")
    )
    expect(page.locator("[data-replay-selection]")).to_contain_text("9007199254740993 raw units")
    wallet.first.press("Escape")
    expect(page.locator(".map-score-center")).to_be_focused()
    expect(page.locator("[data-replay-score-return]")).to_be_hidden()


def test_pending_evidence_keeps_ticker_and_score_visible(page: Page):
    open_replay(page)
    page.route(
        "**/api/memecoins/*/replay*",
        lambda route: route.fulfill(
            json={"status": "pending", "message": "Chain events are being collected."}
        ),
    )
    page.reload()
    expect(page.locator("[data-replay-status]")).to_have_text("Chain events are being collected.")
    expect(page.locator(".map-score-center")).to_be_visible()
    page.route("**/api/memecoins/*/replay*", lambda route: route.fulfill(status=404))
    page.reload()
    expect(page.locator("[data-replay-status]")).to_contain_text("Please retry")
    expect(page.locator(".map-score-center")).to_be_visible()
