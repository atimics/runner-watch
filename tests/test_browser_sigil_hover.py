"""Inspect sigils without changing their evidence or the existing activation path."""

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
    glyph.hover()
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
