from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from runner_web.market_screens import detail, listing

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "indicator_screen_helpers", ROOT / "tests/test_browser_market_screens.py"
)
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
spec = importlib.util.spec_from_file_location(
    "indicator_fixtures", ROOT / "tests/test_stock_indicator.py"
)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
pytestmark = pytest.mark.browser


def examples():
    return [
        fixtures.stock(
            ticker="SMALL",
            score=24,
            stage="WATCH",
            trade_state="WATCH",
            evidence_gate={"state": "gathering"},
        ),
        fixtures.stock(ticker="MED", score=58, rug_level="GUARDED", rug_score=30, sentiment="risk"),
        fixtures.stock(
            ticker="SOLID",
            score=80,
            stage="EXTENDED",
            rug_score=68,
            rug_level="HIGH",
            evidence_gate={"state": "blocked"},
            eligibility={"state": "blocked"},
        ),
        fixtures.stock(
            ticker="UNCERT",
            score=None,
            score_components={},
            rug_score=None,
            rug_level="UNKNOWN",
            sentiment="gap",
            evidence_gate=None,
            eligibility={"state": "unknown"},
        ),
    ]


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_glyphs_fit_dense_list_and_status_chips_are_unchanged(page: Page, width):
    page.set_viewport_size({"width": width, "height": 844})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    helpers.open_screen(page, listing("stocks", examples()))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".ticker>.tag").all_text_contents() == [
        "WATCH",
        "▲RUNNING",
        "▲AVOID",
        "PAUSED",
    ]
    glyphs = page.locator(".indicator-glyph")
    expect(glyphs).to_have_count(4)
    sizes = [glyphs.nth(i).locator(".score-pie").bounding_box()["width"] for i in range(3)]
    assert sizes[0] < sizes[1] == sizes[2]
    masks = [
        glyphs.nth(i).locator(".score-pie").evaluate("el => getComputedStyle(el).maskImage")
        for i in range(3)
    ]
    assert all("gradient" in mask for mask in masks[:2])
    assert masks[2] == "none"
    expect(glyphs.nth(0).locator(".indicator-glyph__risk")).to_have_count(0)
    expect(glyphs.nth(1)).to_have_attribute("data-risk", "medium")
    expect(glyphs.nth(2)).to_have_attribute("data-risk", "high")
    expect(glyphs.nth(3).locator(".indicator-glyph__risk")).to_have_text("?")
    assert (
        glyphs.nth(3)
        .locator(".indicator-glyph__sentiment")
        .evaluate("el => getComputedStyle(el).borderTopStyle")
        == "dashed"
    )
    expect(page.locator(".ticker-verified")).to_have_count(1)
    expect(page.get_by_role("img", name="Verified evidence — automated")).to_be_visible()
    assert page.locator(".ticker-name strong").all_text_contents() == [
        "SMALL",
        "MED",
        "SOLID",
        "UNCERT",
    ]
    for element in page.locator(".ticker").all():
        bounds = [
            element.locator(s).bounding_box()
            for s in [".tag", ".ticker-name", ".ticker-value", ".ticker-score"]
        ]
        assert all(b is not None for b in bounds)
        assert all(
            a["x"] + a["width"] <= b["x"] + 1 for a, b in zip(bounds, bounds[1:], strict=False)
        )
    assert errors == []


def test_detail_explains_colors_and_check_without_hover(page: Page):
    source = fixtures.stock()
    screen = detail(
        "stocks", {"ticker": "GLYPH", "current": source, "evidence_gate": source["evidence_gate"]}
    )
    screen.pop("refresh_url", None)
    screen.pop("chart_url", None)
    helpers.open_screen(page, screen)
    expect(page.get_by_role("region", name="Attention, sentiment and risk")).to_be_visible()
    expect(page.get_by_text("Filing sentiment: Positive · Risk: Low", exact=True)).to_be_visible()
    expect(
        page.locator(".stock-indicator-summary").get_by_text(
            re.compile("Not human review, identity verification")
        )
    ).to_be_visible()
    expect(page.locator("h1 .ticker-verified")).to_be_visible()


def test_full_sentiment_border_and_center_are_independent(page: Page):
    source = fixtures.stock(
        score=88,
        rug_score=70,
        rug_level="HIGH",
        eligibility={"state": "blocked"},
        evidence_gate={"state": "blocked"},
    )
    helpers.open_screen(page, listing("stocks", [source]))
    glyph = page.locator(".indicator-glyph")
    expect(glyph).to_have_attribute("data-sentiment", "positive")
    expect(glyph).to_have_attribute("data-risk", "high")
    styles = glyph.locator(".indicator-glyph__sentiment").evaluate(
        "el => {const s=getComputedStyle(el); return [s.borderTopColor,s.borderRightColor,"
        "s.borderBottomColor,s.borderLeftColor,s.borderTopStyle];}"
    )
    assert len(set(styles[:4])) == 1
    assert styles[4] == "solid"
    assert (
        glyph.locator(".indicator-glyph__risk").evaluate(
            "el => getComputedStyle(el).backgroundColor"
        )
        != styles[0]
    )
    expect(page.locator(".ticker-verified")).to_have_count(0)


def test_markup_refresh_revokes_check_without_relabeling_status(page: Page):
    source = fixtures.stock()
    screen = listing("stocks", [source])
    page.clock.install()
    helpers.open_screen(page, screen)
    next_html = helpers.fixtures.render(
        listing("stocks", [{**source, "evidence_gate": {"state": "gathering"}}])
    )
    page.unroute("http://app.test/")
    page.route(
        "http://app.test/", lambda route: route.fulfill(content_type="text/html", body=next_html)
    )
    page.clock.fast_forward(61000)
    expect(page.locator(".ticker-verified")).to_have_count(0)
    expect(page.locator(".ticker>.tag")).to_have_text("RUNNING")
    expect(page.locator(".indicator-glyph")).to_have_count(1)


def test_detail_refresh_updates_glyph_and_revokes_verification(page: Page):
    source = fixtures.stock()
    first = detail(
        "stocks", {"ticker": "GLYPH", "current": source, "evidence_gate": source["evidence_gate"]}
    )
    page.clock.install()
    helpers.open_screen(page, first)
    expect(page.locator("h1 .ticker-verified")).to_have_count(1)
    changed = {
        **source,
        "score": 80,
        "sentiment": "risk",
        "rug_score": 70,
        "eligibility": {"state": "blocked"},
        "evidence_gate": {"state": "blocked"},
    }
    second = detail(
        "stocks", {"ticker": "GLYPH", "current": changed, "evidence_gate": changed["evidence_gate"]}
    )
    page.route("**/api/screens/stocks/GLYPH/detail", lambda route: route.fulfill(json=second))
    page.clock.fast_forward(61000)
    expect(page.locator("h1 .ticker-verified")).to_have_count(0)
    expect(page.locator(".indicator-glyph")).to_have_attribute("data-band", "3")
    expect(page.locator(".indicator-glyph")).to_have_attribute("data-sentiment", "negative")
    expect(page.locator(".indicator-glyph")).to_have_attribute("data-risk", "high")
    expect(page.locator("[data-indicator-attention]")).to_have_text("Attention 80 points")
    expect(page.locator("[data-indicator-verification-note]")).to_be_hidden()
