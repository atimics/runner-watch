"""Exercise the saved replay on the actual coin details template."""

import json
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
def test_replay_details_layout_history_and_downloads(page: Page, width: int):
    data = open_replay(page, width=width)
    expect(page.locator("[data-node]")).to_have_count(1)
    expect(page.locator("[data-replay-badge]")).to_have_text("Launch recorded")
    page.locator("[data-replay-latest]").click()
    expect(page.locator("[data-replay-graph]")).to_have_attribute("data-phase", "settled")
    expect(page.locator("[data-node]")).to_have_count(len(data["frames"][-1]["nodes"]))
    page.get_by_role("link", name="Save GIF").is_visible()
    expect(page.locator("[data-replay-gif]")).to_have_attribute("href", "/saved.gif")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("[data-replay-origin]").click()
    expect(page.locator("[data-node]")).to_have_count(1)
    expect(page.locator("[data-replay-graph] line")).to_have_count(0)


def test_keyboard_bubble_opens_evidence_and_keeps_focus(page: Page):
    open_replay(page)
    page.locator("[data-replay-latest]").click()
    expect(page.locator("[data-replay-graph]")).to_have_attribute("data-phase", "settled")
    wallet = page.get_by_role("button", name=re.compile(r"^wallet:"))
    wallet.first.focus()
    wallet.first.press("Enter")
    expect(wallet.first).to_be_focused()
    expect(page.locator("[data-replay-selection] a").first).to_have_attribute(
        "href", re.compile("/api/memecoins/evidence/")
    )
    expect(page.locator("[data-replay-selection]")).to_contain_text("9007199254740993 raw units")


def test_replay_loops_and_respects_reduced_motion(page: Page):
    page.emulate_media(reduced_motion="reduce")
    open_replay(page, launch=False)
    expect(page.locator("[data-replay-badge]")).to_have_text("Launch evidence pending")
    page.locator("[data-replay-latest]").click()
    expect(page.locator("[data-replay-graph]")).to_have_attribute("data-phase", "settled")
    page.get_by_role("button", name="Replay events").click()
    expect(page.locator("[data-replay-position]")).to_have_value("0")
    expect(page.get_by_role("button", name="Pause replay")).to_be_visible()
    expect(page.locator("[data-replay-position]")).to_have_value("1", timeout=3000)
    expect(page.locator("[data-replay-position]")).to_have_value("0", timeout=4000)
    page.get_by_role("button", name="Pause replay").click()


def test_pending_evidence_and_failed_revision_show_clear_status(page: Page):
    open_replay(page)
    page.route(
        "**/api/memecoins/*/replay*",
        lambda route: route.fulfill(
            json={
                "status": "pending",
                "message": "The replay will appear as chain evidence is collected.",
            }
        ),
    )
    page.reload()
    expect(page.locator("[data-replay-status]")).to_contain_text("as chain evidence is collected")
    expect(page.locator("[data-replay-content]")).to_be_hidden()
    page.route(
        "**/api/memecoins/*/replay*",
        lambda route: route.fulfill(status=404, body=json.dumps({"detail": "Replay not found"})),
    )
    page.goto("http://app.test/memecoins/coin/" + COIN["id"] + "?replay=" + "0" * 64)
    expect(page.locator("[data-replay-status]")).to_contain_text("awaiting evidence review")
