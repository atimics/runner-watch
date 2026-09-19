"""Receipt details remain readable and open while the market view refreshes."""

import copy

import pytest
from playwright.sync_api import expect

from tests.test_assessment_receipts_review import chain_pattern
from tests.test_browser_market_anchors import open_html, screens

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [320, 390])
def test_pattern_receipts_and_context_survive_refresh(page, width):
    screen, finding = chain_pattern()
    initial = copy.deepcopy(screen)
    initial["item"]["assessment"]["drivers"] = []
    page.set_viewport_size({"width": width, "height": 844})
    page.clock.install()
    open_html(page, screens.render(initial), refresh=screen)
    panel = page.locator(".assessment-finding")
    expect(panel.locator("summary")).to_have_text("Pattern · 4 receipts")
    panel.locator("summary").click()
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
    expect(panel).to_have_attribute("open", "")
    expect(panel.locator("p")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
