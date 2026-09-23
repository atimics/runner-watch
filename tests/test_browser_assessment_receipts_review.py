"""Receipt details remain readable and open while the market view refreshes."""

import pytest
from playwright.sync_api import expect

from tests.test_assessment_receipts_review import chain_pattern
from tests.test_browser_market_anchors import open_html, screens

pytestmark = pytest.mark.browser


def render_map(finding):
    raw = {**screens.sample("memecoins"), "token_address": "A" * 44, "findings": [finding]}
    return screens._render_template(
        "simple_coin_detail.html",
        {"detail": {"coin": raw, "history": []}, "active_call": None, "calls": []},
    )


@pytest.mark.parametrize("width", [320, 390])
def test_pattern_receipts_and_context_survive_refresh(page, width):
    screen, finding = chain_pattern()
    page.set_viewport_size({"width": width, "height": 844})
    page.clock.install()
    open_html(page, render_map(finding), refresh=screen)
    page.locator(".map-event[data-finding-id]").click()
    panel = page.locator("[data-replay-selection]")
    expect(panel.locator("p")).to_have_text(finding["explanation"])
    for index, receipt in enumerate(finding["evidence"]):
        row = panel.locator("li").nth(index)
        expect(row.get_by_role("link", name=f"Swap {index + 1} ↗")).to_have_attribute(
            "href", "https://app.test" + receipt["receipt_url"]
        )
        expect(row.get_by_role("link", name="Explorer ↗")).to_have_attribute(
            "href", receipt["source_url"]
        )
    page.clock.fast_forward(61000)
    expect(panel).to_be_visible()
    expect(panel.locator("p")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("target", ["receipt", "explorer"])
def test_pattern_refresh_keeps_keyboard_focus(page, target):
    screen, finding = chain_pattern()
    page.set_viewport_size({"width": 390, "height": 844})
    page.clock.install()
    open_html(page, render_map(finding), refresh=screen)
    page.locator(".map-event[data-finding-id]").click()
    panel = page.locator("[data-replay-selection]")
    focused = {
        "receipt": panel.get_by_role("link", name="Swap 1 ↗"),
        "explorer": panel.get_by_role("link", name="Explorer ↗").first,
    }[target]
    focused.focus()
    with page.expect_response("**/api/screens/**"):
        page.clock.fast_forward(61000)
    expect(focused).to_be_focused()
    expect(panel).to_be_visible()


def test_removed_finding_moves_focus_to_assessment_heading(page):
    screen, finding = chain_pattern()
    page.clock.install()
    open_html(page, render_map(finding), refresh=screen)
    page.locator(".map-event[data-finding-id]").click()
    page.locator("[data-replay-selection]").get_by_role("link", name="Swap 1 ↗").focus()
    screen["item"]["assessment"]["drivers"] = []
    with page.expect_response("**/api/screens/**"):
        page.clock.fast_forward(61000)
    expect(page.locator(".map-event[data-finding-id]")).to_have_count(0)
    expect(page.locator(".map-score-center")).to_be_focused()
