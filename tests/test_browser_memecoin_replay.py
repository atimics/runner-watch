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


def open_replay(
    page: Page, *, width: int = 390, launch: bool = True, coin_overrides: dict | None = None
) -> dict:
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
    detail = _detail(coin={**_detail()["coin"], **COIN, **(coin_overrides or {})})
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
    # The center names the coin by its address head and tail, never its launch symbol.
    address = COIN["token_address"]
    expect(page.locator(".map-score-center")).to_contain_text(address[:6] + "…" + address[-6:])
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


@pytest.mark.parametrize("width,score,band", [(320, 24, "1"), (390, 58, "2"), (1440, 80, "3")])
def test_token_glyph_shares_stock_shapes_and_opens_each_reading(page: Page, width, score, band):
    from tests.test_memecoin_indicator import assessed_coin

    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    open_replay(page, width=width, coin_overrides=assessed_coin(id=COIN["id"], score=score))
    glyph = page.locator(".map-glyph")
    expect(glyph).to_have_attribute("data-band", band)
    expect(glyph).to_have_attribute("data-sentiment", "negative")
    expect(glyph).to_have_attribute("data-risk", "detected")
    expect(glyph.locator(".map-score-segment")).to_have_count(3)
    segment = glyph.locator('[data-score-key="evidence"]')
    assert segment.evaluate("el => getComputedStyle(el).fill") == (
        "rgb(181, 138, 244)" if band == "3" else "none"
    )
    expect(glyph.locator(".map-glyph-hole")).to_have_count(0 if band == "3" else 1)
    segment.focus()
    segment.press("Enter")
    panel = page.locator("[data-replay-selection]")
    expect(panel.locator("h4")).to_have_text("Chain evidence")
    expect(panel).to_contain_text("20 pts")
    segment.press("End")
    risk = glyph.locator('[data-score-key="risk"]')
    expect(risk).to_be_focused()
    risk.press("Space")
    expect(panel).to_contain_text("Risk factors detected in saved checks.")
    risk.press("ArrowLeft")
    tone = glyph.locator('[data-score-key="sentiment"]')
    expect(tone).to_be_focused()
    tone.press("Enter")
    expect(panel).to_contain_text(
        "0% bullish, 100% bearish. Saved chain evidence tone assessments."
    )
    tone.press("Escape")
    expect(page.locator(".map-score-center")).to_be_focused()
    expect(page.locator("[data-replay-score-return]")).to_be_hidden()
    page.get_by_text("Indicator key", exact=True).click()
    expect(page.locator(".indicator-key-group").filter(has_text="Attention")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_quote_only_ring_explains_unknown_values(page: Page):
    open_replay(page)
    glyph = page.locator(".map-glyph")
    expect(glyph).to_have_attribute("data-mix", "unknown")
    expect(glyph.locator(".map-score-segment")).to_have_count(0)
    expect(glyph.locator(".map-risk-unknown")).to_have_text("?")
    glyph.locator('[data-score-key="risk"]').click()
    expect(page.locator("[data-replay-selection]")).to_contain_text(
        "Risk factor checks unavailable."
    )
    expect(page.locator("[data-replay-selection]")).to_contain_text("Attention unavailable")


def test_glyph_refresh_preserves_selection_and_clears_removed_assessment(page: Page):
    from tests.test_memecoin_indicator import assessed_coin

    page.clock.install()
    coin = assessed_coin(id=COIN["id"])
    open_replay(page, coin_overrides=coin)
    risk = page.locator('[data-score-key="risk"]')
    risk.click()
    changed = {**coin, "rug_score": 70, "chain_sentiment": "positive"}
    page.route(
        "**/api/screens/**",
        lambda route: route.fulfill(
            json=main.simple_market_detail("memecoins", _detail(coin=changed))
        ),
    )
    page.clock.fast_forward(61000)
    expect(risk).to_be_focused()
    expect(risk).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".map-glyph")).to_have_attribute("data-sentiment", "positive")
    expect(page.locator("[data-replay-selection]")).to_contain_text("Risk factors detected")
    page.route(
        "**/api/screens/**",
        lambda route: route.fulfill(
            json=main.simple_market_detail("memecoins", _detail(coin={**_detail()["coin"], **COIN}))
        ),
    )
    page.clock.fast_forward(61000)
    expect(page.locator(".map-glyph")).to_have_attribute("data-mix", "unknown")
    expect(page.locator(".map-score-segment")).to_have_count(0)
    expect(page.locator("[data-replay-selection]")).to_contain_text("Attention unavailable")
    expect(page.locator("[data-replay-selection]")).to_contain_text(
        "Risk factor checks unavailable"
    )


@pytest.mark.parametrize("width", [390, 1280])
def test_token_sentiment_uses_the_shared_proportional_border(page, width):
    from tests.test_memecoin_indicator import assessed_coin

    open_replay(
        page,
        width=width,
        coin_overrides=assessed_coin(
            id=COIN["id"], chain_sentiment_counts={"bullish": 1, "bearish": 3}
        ),
    )
    glyph = page.locator(".map-glyph")
    expect(glyph.locator('[data-sentiment-side="bullish"]')).to_have_attribute(
        "stroke-dasharray", "25 75"
    )
    expect(glyph.locator('[data-sentiment-side="bearish"]')).to_have_attribute(
        "stroke-dashoffset", "-25"
    )
    glyph.locator('[data-score-key="sentiment"]').press("Enter")
    expect(page.locator("[data-replay-selection]")).to_contain_text("25% bullish, 75% bearish")
