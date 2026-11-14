"""Inspect sigils without changing their evidence or the existing activation path."""

import re

import pytest
from playwright.sync_api import Page, expect

from runner_web.market_screens import listing
from tests.test_browser_market_screens import open_screen
from tests.test_browser_memecoin_replay import open_replay
from tests.test_stock_indicator import stock

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [320, 1280])
def test_hover_pill_can_be_read_and_dismissed_without_changing_glyph(page: Page, width):
    page.set_viewport_size({"width": width, "height": 844})
    open_screen(page, listing("stocks", [stock(score=24)]))
    glyph = page.locator(".indicator-glyph")
    before = glyph.bounding_box()
    description = glyph.get_attribute("aria-label")
    glyph.hover(position={"x": before["width"] / 2, "y": 1})
    pill = page.get_by_role("tooltip")
    expect(pill).to_have_text("Attention · 24/100")
    expect(glyph).to_have_attribute("data-sigil-lit", "")
    assert glyph.bounding_box() == before
    assert glyph.get_attribute("aria-label") == description
    assert glyph.get_attribute("title") is None
    bounds = pill.bounding_box()
    assert bounds["x"] >= 12 and bounds["x"] + bounds["width"] <= width - 11
    pill.hover()
    page.wait_for_timeout(200)
    expect(pill).to_be_visible()
    page.keyboard.press("Escape")
    expect(pill).to_be_hidden()
    assert page.url == "http://app.test/"


def test_row_focus_and_replacement_keep_accessible_inspection(page: Page):
    page.emulate_media(reduced_motion="reduce")
    open_screen(page, listing("stocks", [stock(score=None)]))
    row = page.locator("a.ticker")
    row.evaluate("el => el.setAttribute('aria-describedby', 'existing-description')")
    row.focus()
    expect(page.get_by_role("tooltip")).to_have_text("Attention · unavailable")
    expect(row).to_have_attribute("aria-describedby", "existing-description sigil-tooltip")
    expect(row.locator(".indicator-glyph")).to_have_css("transition-duration", "0s")
    row.press("Escape")
    expect(row).to_be_focused()
    expect(row).to_have_attribute("aria-describedby", "existing-description")
    expect(page.get_by_role("tooltip")).to_be_hidden()
    row.evaluate("el => el.replaceWith(el.cloneNode(true))")
    row.hover(position={"x": 2, "y": 2})
    row.locator(".indicator-glyph").hover()
    expect(page.get_by_role("tooltip")).to_be_visible()
    row.evaluate("el => el.remove()")
    expect(page.get_by_role("tooltip")).to_be_hidden()


def test_memecoin_part_preview_preserves_selection_and_focus(page: Page, tmp_path):
    open_replay(page, width=1440)
    graph = page.locator("[data-replay-graph]")
    graph.evaluate("el => el.setAttribute('tabindex', '0')")
    graph.focus()
    expect(page.get_by_role("tooltip")).to_be_hidden()
    part = page.locator('.map-glyph [data-score-key="liquidity"]')
    part.focus()
    pill = page.get_by_role("tooltip")
    expect(pill).to_contain_text("Real liquidity")
    expect(part).to_have_attribute("data-sigil-lit", "")
    page.screenshot(path=str(tmp_path / "sigil-focus.png"), full_page=True)
    part.press("Escape")
    expect(pill).to_be_hidden()
    expect(part).to_be_focused()
    part.press("Enter")
    expect(part).to_have_attribute("aria-pressed", "true")
    expect(page.locator("[data-replay-selection]")).to_contain_text("Liquidity")
    expect(pill).to_be_hidden()
    part.press("Escape")
    expect(page.locator(".map-score-center")).to_be_focused()


def test_forced_colors_keeps_outline_and_touch_keeps_navigation(browser):
    context = browser.new_context(
        has_touch=True, is_mobile=True, viewport={"width": 390, "height": 844}
    )
    page = context.new_page()
    try:
        open_screen(page, listing("stocks", [stock(score=24)]))
        target = page.locator("a.ticker")
        href = target.get_attribute("href")
        page.route("http://app.test/stock/**", lambda route: route.fulfill(body="Opened details"))
        target.locator(".indicator-glyph").tap()
        expect(page).to_have_url("http://app.test" + href)
        expect(page.get_by_role("tooltip")).to_be_hidden()
        open_screen(page, listing("stocks", [stock(score=24)]))
        page.emulate_media(forced_colors="active")
        page.locator("a.ticker").focus()
        expect(page.locator(".indicator-glyph")).to_have_css("filter", "none")
        expect(page.locator(".indicator-glyph")).to_have_css("outline-width", "2px")
    finally:
        context.close()


def hover_polar(page, locator, fraction, radius_ratio):
    """Move the real pointer to a rendered mark, clockwise from twelve o'clock."""
    point = locator.evaluate(
        """(el, [fraction, ratio]) => {
          const rect = el.getBoundingClientRect(), angle = fraction * Math.PI * 2 - Math.PI / 2;
          const radius = rect.width / 2 * ratio;
          return {x:rect.x + rect.width / 2 + radius * Math.cos(angle),
                  y:rect.y + rect.height / 2 + radius * Math.sin(angle)};
        }""",
        [fraction, radius_ratio],
    )
    page.mouse.move(**point)


def hover_svg_stroke(page, locator):
    point = locator.evaluate(
        """el => {
          const local = el.getPointAtLength(el.getTotalLength() / 2);
          const point = new DOMPoint(local.x, local.y).matrixTransform(el.getScreenCTM());
          return {x:point.x,y:point.y};
        }"""
    )
    page.mouse.move(**point)


@pytest.mark.parametrize("width", [390, 1280])
def test_stock_list_reads_each_contribution_sentiment_and_risk(page: Page, width):
    page.set_viewport_size({"width": width, "height": 844})
    source = stock(
        score=58,
        score_components={"market": 40, "sec_event": 30, "social_search": 20, "cluster": 10},
        sentiment_counts={"bullish": 0, "bearish": 1, "filings": 3},
    )
    open_screen(page, listing("stocks", [source]))
    glyph = page.locator(".indicator-glyph")
    for fraction, key, text in [
        (0.2, "market", "Market · 40 points · 40% of contributions"),
        (0.55, "evidence", "Filings + news · 30 points · 30% of contributions"),
        (0.8, "social", "External social · 20 points · 20% of contributions"),
        (0.95, "cluster", "Cluster holdings · 10 points · 10% of contributions"),
    ]:
        hover_polar(page, glyph.locator(".score-pie"), fraction, 0.84)
        expect(page.get_by_role("tooltip")).to_have_text(text)
        expect(glyph).to_have_attribute("data-sigil-part", key)
    hover_polar(page, glyph.locator(".indicator-glyph__face"), 0.25, 0.94)
    expect(page.get_by_role("tooltip")).to_contain_text("1 of 3 filings flagged bearish")
    glyph.locator(".indicator-glyph__risk").hover()
    expect(page.get_by_role("tooltip")).to_contain_text("Risk factors")
    expect(glyph).to_have_attribute("data-sigil-part", "risk")
    expect(glyph.locator("a,button,[tabindex]")).to_have_count(0)


@pytest.mark.parametrize("width", [390, 1280])
def test_well_list_reads_actual_svg_marks(page: Page, width):
    from tests.test_memecoin_indicator import assessed_coin

    page.set_viewport_size({"width": width, "height": 844})
    coin = assessed_coin(
        score=58,
        venue="pool",
        real_liquidity_usd=10000,
        liquidity_usd=30000,
        fully_diluted_valuation=1000000,
        liquidity_lock={"left_pct": 40.0, "dex": "PumpSwap"},
        buyers_h1=30,
        sellers_h1=10,
        ratification={
            "standards": [
                {"key": "pool", "label": "Pool", "met": True},
                {"key": "age", "label": "Age", "met": False},
                {"key": "holders", "label": "Holders", "met": None},
            ]
        },
    )
    open_screen(page, listing("memecoins", [coin]))
    glyph = page.locator(".indicator-glyph--well")
    for key, label, points in [
        ("market", "Market", 28),
        ("evidence", "Chain evidence", 20),
        ("social", "External social", 10),
    ]:
        hover_svg_stroke(page, glyph.locator(f'.well-slice[data-key="{key}"]'))
        expect(page.get_by_role("tooltip")).to_contain_text(f"{label} · {points} points")
    for selector, text in [
        (".well-bull", "Last hour · 30 buyers"),
        (".well-bear", "Last hour · 10 sellers"),
        ('.well-standard[data-state="met"]', "Pool · Met"),
        ('.well-standard[data-state="unmet"]', "Age · Not met"),
        ('.well-standard[data-state="unchecked"]', "Holders · Not checked yet"),
        (".well-wall", "40% of liquidity tokens still held"),
        (".well-ghost", "Quoted liquidity · $30,000; real liquidity · $10,000"),
    ]:
        hover_svg_stroke(page, glyph.locator(selector))
        expect(page.get_by_role("tooltip")).to_have_text(text)
    glyph.locator(".well-water").hover()
    expect(page.get_by_role("tooltip")).to_have_text("Real liquidity · $10,000")
    glyph.locator(".well-risk").hover()
    expect(page.get_by_role("tooltip")).to_contain_text("Risk factors detected")
    expect(glyph.locator("a,button,[tabindex]")).to_have_count(0)
    page.keyboard.press("Tab")
    page.locator("a.ticker").focus()
    expect(page.get_by_role("tooltip")).to_have_text("Attention · 58/100")
    expect(glyph).to_have_attribute("aria-label", re.compile("Real liquidity"))
