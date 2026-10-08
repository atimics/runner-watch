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
    expect(glyphs.nth(0).locator(".indicator-glyph__risk")).to_have_count(1)
    expect(glyphs.nth(1)).to_have_attribute("data-risk", "detected")
    expect(glyphs.nth(2)).to_have_attribute("data-risk", "detected")
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
            for s in [
                ".ticker-trend" if width <= 760 else ".tag",
                ".ticker-name",
                ".ticker-value",
                ".ticker-score",
            ]
        ]
        assert all(b is not None for b in bounds)
        assert all(
            a["x"] + a["width"] <= b["x"] + 1 for a, b in zip(bounds, bounds[1:], strict=False)
        )
    assert errors == []


def test_detail_keeps_header_check_without_duplicate_attention_card(page: Page):
    source = fixtures.stock()
    screen = detail(
        "stocks", {"ticker": "GLYPH", "current": source, "evidence_gate": source["evidence_gate"]}
    )
    screen.pop("refresh_url", None)
    screen.pop("chart_url", None)
    helpers.open_screen(page, screen)
    expect(page.locator(".stock-indicator-summary")).to_have_count(0)
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
    expect(glyph).to_have_attribute("data-risk", "detected")
    expect(glyph).to_have_attribute("data-sentiment-mix", "available")
    border = glyph.locator(".indicator-glyph__sentiment")
    assert "100%" in border.get_attribute("style")
    assert "conic-gradient" in border.evaluate("el => getComputedStyle(el).backgroundImage")
    assert "radial-gradient" in border.evaluate("el => getComputedStyle(el).maskImage")
    expect(glyph).to_have_attribute("aria-label", re.compile("100% bullish, 0% bearish"))
    expect(glyph.locator(".indicator-glyph__risk")).to_have_css(
        "background-color", "rgb(239, 153, 164)"
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


def test_detail_refresh_revokes_header_verification_without_summary(page: Page):
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
    expect(page.locator(".stock-indicator-summary")).to_have_count(0)


@pytest.mark.parametrize("width", [320, 1280])
def test_sentiment_shares_and_neutral_gap_survive_markup_refresh(page, width):
    page.set_viewport_size({"width": width, "height": 844})
    page.clock.install()
    source = fixtures.stock(sentiment_counts={"bullish": 3, "bearish": 1})
    helpers.open_screen(page, listing("stocks", [source]))
    glyph = page.locator(".indicator-glyph")
    border = glyph.locator(".indicator-glyph__sentiment")
    expect(glyph).to_have_attribute("aria-label", re.compile("75% bullish, 25% bearish"))
    assert "75.000000%" in border.get_attribute("style")
    expect(border).to_have_css(
        "background-image",
        "conic-gradient(rgb(165, 229, 185) 0%, rgb(165, 229, 185) 75%, "
        "rgb(239, 153, 164) 75%, rgb(239, 153, 164) 100%)",
    )
    next_html = helpers.fixtures.render(
        listing("stocks", [{**source, "sentiment_counts": {"bullish": 0, "bearish": 0}}])
    )
    page.unroute("http://app.test/")
    page.route(
        "http://app.test/", lambda route: route.fulfill(content_type="text/html", body=next_html)
    )
    page.clock.fast_forward(61000)
    expect(glyph).to_have_attribute("data-sentiment-mix", "unknown")
    expect(page.locator(".ticker-sentiment")).to_have_count(0)  # no text readout on the row
    expect(glyph.locator("[data-bearish-pattern]")).to_have_count(0)
    expect(border).to_have_css("border-top-style", "dashed")
    expect(border).to_have_css("background-image", "none")


# Memecoins draw the Well instead: see the Well tests at the end of this file.
@pytest.mark.parametrize("market", ["stocks"])
@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("forced", ["none", "active"])
def test_visible_sentiment_patterns_and_risk_shapes_in_both_color_modes(
    page, market, width, forced
):
    from tests.test_memecoin_indicator import assessed_coin

    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(forced_colors=forced)
    make = fixtures.stock if market == "stocks" else assessed_coin
    counts_key = "sentiment_counts" if market == "stocks" else "chain_sentiment_counts"
    source = [
        make(
            ticker="MED",
            id="MED",
            **{counts_key: {"bullish": 3, "bearish": 1}},
            rug_score=30,
            significant_risk_factor_count=1,
        ),
        make(ticker="HIGH", id="HIGH", **{counts_key: {"bullish": 0, "bearish": 4}}, rug_score=75),
        make(
            ticker="GAP",
            id="GAP",
            **{counts_key: {"bullish": 0, "bearish": 0}},
            rug_score=None,
            rug_level="unknown",
        ),
    ]
    helpers.open_screen(page, listing(market, source))
    # The rows carry no ▲/▼ text readout; the glyph's outer ring is the reading.
    expect(page.locator(".ticker-sentiment")).to_have_count(0)
    glyphs = page.locator(".indicator-glyph")
    expect(glyphs.nth(0).locator("[data-bearish-pattern]")).to_be_visible()
    expect(glyphs.nth(1).locator("[data-bearish-pattern]")).to_be_visible()
    expect(glyphs.nth(2).locator("[data-bearish-pattern]")).to_have_count(0)
    expect(glyphs.nth(2).locator(".indicator-glyph__sentiment")).to_have_css(
        "border-top-style", "dashed"
    )
    expect(glyphs.nth(0).locator(".indicator-glyph__risk")).to_have_css("border-radius", "50%")
    expect(glyphs.nth(1).locator(".indicator-glyph__risk")).to_have_css("border-radius", "0px")
    expect(glyphs.nth(2).locator(".indicator-glyph__risk")).to_have_text("?")
    for pattern in glyphs.nth(0).locator(".score-pie [data-pattern]").all():
        expect(pattern).to_be_visible()
        assert "gradient" in pattern.evaluate("el => getComputedStyle(el).backgroundImage")
        assert "conic-gradient" in pattern.evaluate("el => getComputedStyle(el).maskImage")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("market", ["stocks"])
@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("forced", ["none", "active"])
def test_visual_key_opens_by_keyboard_and_keeps_patterns_and_labels(page, market, width, forced):
    from tests.test_memecoin_indicator import assessed_coin

    page.set_viewport_size({"width": width, "height": 1100})
    page.emulate_media(forced_colors=forced)
    source = fixtures.stock() if market == "stocks" else assessed_coin()
    helpers.open_screen(page, listing(market, [source]))
    key = page.locator(".indicator-legend")
    key.locator("summary").focus()
    key.locator("summary").press("Enter")
    expect(key).to_have_attribute("open", "")
    sentiment_title = "Observed swap balance" if market == "memecoins" else "Sentiment"
    expect(key.locator(".indicator-key-group h3")).to_have_text(
        ["Risk", "Attention", sentiment_title]
    )
    if market == "memecoins":
        expect(key.get_by_text("▲ Net buying wallets", exact=False)).to_be_visible()
        expect(key.get_by_text("▼ Net selling wallets", exact=False)).to_be_visible()
    expect(key.get_by_text("Risk factors detected", exact=False)).to_be_visible()
    expect(key.get_by_text("0 factors detected", exact=False)).to_be_visible()
    expect(key.get_by_text("Checks unavailable", exact=False)).to_be_visible()
    for kind in [
        "market",
        "evidence",
        "social",
        "bullish",
        "bearish",
        "sentiment",
        "detected",
        "significant",
        "none",
        "unknown",
        "unavailable",
    ]:
        icon = key.locator(f'[data-key-icon="{kind}"]')
        expect(icon).to_be_visible()
        assert icon.bounding_box()["width"] >= 24
    for kind in ["evidence", "social", "bearish"]:
        pattern = key.locator(f'[data-key-icon="{kind}"] .indicator-pattern')
        assert "gradient" in pattern.evaluate("el => getComputedStyle(el).backgroundImage")
    expect(key.locator('[data-key-icon="unavailable"] .indicator-key-ring')).to_have_css(
        "border-top-style", "dashed"
    )
    expect(key.locator('[data-key-icon="detected"] .indicator-key-marker')).to_have_css(
        "border-radius", "0px"
    )
    expect(key.get_by_text("1+ significant factors", exact=False)).to_be_visible()
    expect(key.locator('[data-key-icon="significant"] .indicator-key-marker')).to_have_css(
        "border-radius", "50%"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not re.search(r"\b(low|medium|high|critical) risk\b", key.inner_text(), re.I)
    if forced == "active":
        expect(key.locator('[data-key-icon="evidence"]')).to_have_css("forced-color-adjust", "none")


@pytest.mark.parametrize("market", ["stocks"])
@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("forced", ["none", "active"])
def test_significant_factor_circle_keeps_accessible_map_control(page, market, width, forced):
    from tests.test_browser_memecoin_replay import COIN, open_replay
    from tests.test_browser_stock_map import open_map
    from tests.test_memecoin_indicator import assessed_coin
    from tests.test_stock_map import score_current

    page.emulate_media(forced_colors=forced)
    if market == "stocks":
        open_map(page, width, current=score_current(rug_score=0, hard_veto=True))
    else:
        open_replay(
            page,
            width=width,
            coin_overrides=assessed_coin(id=COIN["id"], significant_risk_factor_count=1),
        )
    glyph = page.locator(".map-glyph")
    expect(glyph).to_have_attribute("data-risk", "significant")
    expect(glyph.locator("circle.map-risk-dot")).to_have_attribute("data-risk-shape", "circle")
    risk = glyph.locator('[data-score-key="risk"]')
    expect(risk).to_have_attribute(
        "aria-label", "1+ significant risk factors detected in saved checks. Show risk factors."
    )
    risk.press("Enter")
    expect(risk).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".map-glyph-reading")).to_contain_text(
        "1+ significant risk factors detected"
    )
    if forced == "active":
        expect(glyph.locator(".map-risk-dot")).to_have_css("fill", "rgb(0, 0, 0)")
    else:
        expect(glyph.locator(".map-risk-dot")).to_have_css("fill", "rgb(255, 173, 112)")


# ------------------------------------------------------------------ the Well
# A memecoin's sigil is drawn around its liquidity (memecoin_well.py).


@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("forced", ["none", "active"])
def test_well_rows_show_trading_standards_risk_and_liquidity(page, width, forced):
    from tests.test_memecoin_indicator import assessed_coin

    page.set_viewport_size({"width": width, "height": 844})
    page.emulate_media(forced_colors=forced)
    pooled = {
        "venue": "pool",
        "real_liquidity_usd": 72.88,
        "liquidity_usd": 2174.0,
        "fully_diluted_valuation": 2135.0,
        "liquidity_lock": {"left_pct": 0.0, "dex": "PumpSwap"},
    }
    source = [
        assessed_coin(
            id="MED",
            buyers_h1=30,
            sellers_h1=10,
            ratification={
                "standards": [
                    {"key": "pool", "label": "Pool", "met": True},
                    {"key": "age", "label": "Age", "met": False},
                    {"key": "holders", "label": "Holders", "met": None},
                ]
            },
            rug_score=30,
            significant_risk_factor_count=1,
            **pooled,
        ),
        assessed_coin(id="HIGH", buyers_h1=0, sellers_h1=4, rug_score=75),
        assessed_coin(
            id="GAP",
            buyers_h1=0,
            sellers_h1=0,
            rug_score=None,
            rug_level="unknown",
        ),
    ]
    helpers.open_screen(page, listing("memecoins", source))
    glyphs = page.locator(".indicator-glyph--well")
    expect(glyphs).to_have_count(3)
    first, second, third = (glyphs.nth(i) for i in range(3))
    expect(first.locator(".well-bull")).to_have_count(1)
    expect(first.locator(".well-bear")).to_have_count(1)
    expect(second.locator(".well-bull")).to_have_count(0)
    expect(third.locator(".well-bull, .well-bear")).to_have_count(0)
    expect(first.locator("circle.well-risk")).to_have_attribute("data-risk", "significant")
    expect(second.locator("path.well-risk")).to_have_attribute("data-risk", "detected")
    expect(third.locator(".well-risk")).to_have_count(0)
    # Standards ticks: one each for met, failed and not checked yet.
    for state in ["met", "unmet", "unchecked"]:
        expect(first.locator(f'.well-standard[data-state="{state}"]')).to_have_count(1)
    expect(second.locator(".well-standard")).to_have_count(0)
    # A thin pool reads coral; the sealed wall draws no rim.
    expect(first.locator(".well-water")).to_have_attribute("data-depth", "thin")
    if forced == "none":
        expect(first.locator(".well-water")).to_have_css("fill", "rgb(239, 135, 120)")
        expect(first.locator(".well-wall")).to_have_css("stroke", "none")
    expect(glyphs.locator(".well-score-label")).to_have_text(["58", "58", "58"])
    # A phantom pool, sealed: $72.88 real under a $2,174 quote.
    expect(first).to_have_attribute("data-lock", "sealed")
    expect(first.locator(".well-phantom")).to_have_count(1)
    expect(first).to_have_accessible_name(
        re.compile(
            r"^Real liquidity \$73; the pool quotes \$2.2K, 30×.*"
            r"Last hour: 30 buyers, 10 sellers \(75% buyers\)\. Standards: 1 of 3 met"
        )
    )
    expect(second.locator(".well-unknown")).to_have_text("?")
    box = first.locator("svg").bounding_box()
    assert box["width"] >= 40 and box["height"] >= 40
    if forced == "active":
        expect(first).to_have_css("forced-color-adjust", "none")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("forced", ["none", "active"])
def test_well_key_opens_by_keyboard_and_is_drawn_by_the_row_code(page, width, forced):
    from tests.test_memecoin_indicator import assessed_coin

    page.set_viewport_size({"width": width, "height": 1100})
    page.emulate_media(forced_colors=forced)
    helpers.open_screen(page, listing("memecoins", [assessed_coin()]))
    key = page.locator(".indicator-legend")
    key.locator("summary").focus()
    key.locator("summary").press("Enter")
    expect(key).to_have_attribute("open", "")
    expect(key.locator(".indicator-key-group h3")).to_have_text(
        ["Liquidity", "Wall", "Standards", "Attention and trading"]
    )
    for label in [
        "Deep",
        "Fair",
        "Thin",
        "Several pools",
        "Phantom pool",
        "Ratified",
        "82 of 100",
        "75% buyers",
        "Open",
        "Bonding curve",
        "Liquidity pull",
        "Risk factors detected",
        "1+ significant factors",
    ]:
        expect(key.locator("li").filter(has_text=label).first).to_be_visible()
    icons = key.locator(".indicator-key-icon--well svg.well-glyph")
    expect(icons).to_have_count(18)
    assert all(icon.bounding_box()["width"] >= 24 for icon in icons.all())
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("forced", ["none", "active"])
def test_well_significant_notch_keeps_accessible_map_control(page, width, forced):
    from tests.test_browser_memecoin_replay import COIN, open_replay
    from tests.test_memecoin_indicator import assessed_coin

    page.emulate_media(forced_colors=forced)
    open_replay(
        page,
        width=width,
        coin_overrides=assessed_coin(id=COIN["id"], significant_risk_factor_count=1),
    )
    glyph = page.locator(".map-glyph")
    expect(glyph).to_have_attribute("data-risk", "significant")
    expect(glyph.locator("circle.well-risk")).to_have_attribute("data-risk-shape", "circle")
    risk = glyph.locator('[data-score-key="risk"]')
    expect(risk).to_have_attribute(
        "aria-label", "1+ significant risk factors detected in saved checks. Show risk factors."
    )
    risk.press("Enter")
    expect(risk).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".map-glyph-reading")).to_contain_text(
        "1+ significant risk factors detected"
    )
    if forced == "active":
        expect(glyph.locator(".well-risk")).to_have_css("fill", "rgb(0, 0, 0)")
    else:
        expect(glyph.locator(".well-risk")).to_have_css("fill", "rgb(255, 173, 112)")
