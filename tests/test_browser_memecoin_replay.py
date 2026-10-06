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
    # With no chain finding saved, the evidence block stays out of the way.
    expect(page.locator("[data-market-assessment]")).to_be_hidden()
    expect(page.locator("[data-replay-gif], .replay-controls")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


@pytest.mark.parametrize("width", [390, 1440])
def test_wallets_orbit_one_ring_without_paging(page: Page, width: int):
    data = open_replay(page, width=width)
    graph = page.locator("[data-replay-graph]")
    wallets = len([n for n in data["frames"][-1]["nodes"] if n["id"] != "launch"])
    expect(page.locator("[data-replay-graph] [data-node]:not([data-node=launch])")).to_have_count(
        wallets
    )
    expect(page.locator("[data-replay-paging], [data-replay-next]")).to_have_count(0)
    expect(graph).to_have_attribute("data-layout", "spiral" if wallets > 8 else "ring")
    expect(graph).to_have_attribute("data-orbit-track", "150,150" if width < 500 else "270,150")
    anchors = page.locator("[data-replay-graph] [data-orbit-anchor]")
    expect(anchors).to_have_count(wallets)


def test_keyboard_bubble_opens_evidence_and_returns_to_score(page: Page):
    open_replay(page, width=1440)
    wallet = page.get_by_role("button", name=re.compile(r"^wallet:"))
    wallet.first.focus()
    wallet.first.press("Enter")
    expect(wallet.first).to_be_focused()
    expect(
        page.locator('[data-replay-selection] a[href^="/api/memecoins/evidence/"]').first
    ).to_have_attribute("href", re.compile("/api/memecoins/evidence/"))
    expect(page.get_by_role("link", name="Open wallet · PnL ↗")).to_have_attribute(
        "href", re.compile(r"^/wallets/solana/[1-9A-HJ-NP-Za-km-z]{32,44}$")
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


POOLED = {
    "venue": "pool",
    "real_liquidity_usd": 900.0,
    "liquidity_usd": 10_000.0,
    "fully_diluted_valuation": 500_000.0,
    "liquidity_lock": {"left_pct": 40.0, "dex": "Raydium CPMM"},
}


@pytest.mark.parametrize("width,score,band", [(320, 24, "1"), (390, 58, "2"), (1440, 80, "3")])
def test_token_well_draws_liquidity_and_opens_each_reading(page: Page, width, score, band):
    from tests.test_memecoin_indicator import assessed_coin

    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    open_replay(
        page, width=width, coin_overrides=assessed_coin(id=COIN["id"], score=score, **POOLED)
    )
    glyph = page.locator(".map-glyph")
    expect(glyph).to_have_attribute("data-band", band)
    expect(glyph).to_have_attribute("data-well", "")
    expect(glyph).to_have_attribute("data-lock", "open")
    expect(glyph).to_have_attribute("data-risk", "detected")
    # Attention is the frame: every band keeps its slices as strokes, never a solid pie.
    expect(glyph.locator(".map-score-segment")).to_have_count(3)
    segment = glyph.locator('[data-score-key="evidence"]')
    assert segment.evaluate("el => getComputedStyle(el).stroke") == "rgb(181, 138, 244)"
    # A pool quoting 11× its real liquidity shows the phantom between ghost and water.
    expect(glyph.locator(".well-water")).to_have_count(1)
    expect(glyph.locator(".well-phantom")).to_have_count(1)
    expect(glyph.locator('.well-wall[data-lock="open"]')).to_have_count(1)
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
    well = glyph.locator('[data-score-key="liquidity"]')
    expect(well).to_be_focused()
    well.press("Enter")
    expect(panel.locator("h4")).to_have_text("Liquidity")
    expect(panel).to_contain_text("the pool quotes $10.0K, 11×")
    expect(panel).to_contain_text("40% still held")
    well.press("ArrowLeft")
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
    expect(page.locator(".indicator-key-group").filter(has_text="Phantom pool")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_quote_only_well_explains_unknown_values(page: Page):
    open_replay(page)
    glyph = page.locator(".map-glyph")
    expect(glyph).to_have_attribute("data-mix", "unknown")
    expect(glyph.locator(".map-score-segment")).to_have_count(0)
    # Unknown liquidity is a question in the centre; unknown risk draws nothing.
    expect(glyph.locator(".well-unknown")).to_have_text("?")
    expect(glyph.locator(".well-water")).to_have_count(0)
    expect(glyph.locator('[data-score-key="risk"]')).to_have_count(0)
    glyph.locator('[data-score-key="liquidity"]').click()
    panel = page.locator("[data-replay-selection]")
    expect(panel).to_contain_text("Real liquidity not read")
    expect(panel).to_contain_text("liquidity lock not checked yet")
    expect(panel).not_to_contain_text("unavailable ·")
    expect(page.locator(".map-glyph-reading")).to_have_count(0)


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
    expect(page.locator("[data-replay-selection]")).not_to_contain_text("unavailable ·")
    expect(page.locator(".map-glyph-reading")).to_have_count(0)
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
