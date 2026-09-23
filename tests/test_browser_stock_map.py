import json
import math
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from runner_web import main
from runner_web.stock_map import filing_events
from tests.test_browser_ticker import _request
from tests.test_stock_map import evidence, filing_row, score_current

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parents[1]


def map_payload():
    events = []
    for index in range(10):
        data = evidence()
        data["owners"] = [{"cik": 101 + index, "name": f"Person {index}", "role": "Director"}]
        data["transactions"] = [data["transactions"][index % 4]]
        events.extend(
            filing_events(
                filing_row(
                    str(index),
                    evidence_json=json.dumps(data),
                    filed_at=f"2026-09-{index + 1:02}T18:00:00+00:00",
                )
            )
        )
    events.extend(
        filing_events(
            filing_row(
                "stake",
                form="SCHEDULE 13D/A",
                evidence_json=json.dumps(
                    {
                        "positions": [
                            {
                                "name": "Fund, LP",
                                "cik": 999,
                                "percent": 7.5,
                                "shares": 5000,
                                "security": "Class A",
                                "occurred_at": "2026-09-01",
                            }
                        ]
                    }
                ),
            )
        )
    )
    return {
        "ticker": "TEST",
        "events": events,
        "loaded_filings": 11,
        "coverage": {"filings": 12},
        "next_cursor": "older",
    }


def open_map(
    page: Page,
    width=1280,
    *,
    current=None,
    map_handler=None,
    states=None,
    history=None,
    connections_handler=None,
):
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    request = _request()
    detail = {
        "ticker": "TEST",
        "company": "Test Company",
        "current": score_current() if current is None else current,
        "events": [],
    }
    if states is not None:
        detail["states"] = states
    if history is not None:
        detail["history"] = history
    screen = main.simple_market_detail("stocks", detail)
    if history is None:
        screen["series"] = [
            {"time": f"2026-09-{day:02}T18:00:00Z", "value": day + 2} for day in range(1, 12)
        ]
    html = main.templates.TemplateResponse(
        request,
        "simple_stock_detail.html",
        main.page_context(
            request, None, resolved_user=None, detail=detail, active_call=None, calls=[]
        ),
    ).body.decode()
    html = re.sub(
        r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
        lambda m: "<style>" + (ROOT / "web/static" / m[1]).read_text() + "</style>",
        html,
    )
    html = re.sub(
        r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>',
        lambda m: "<script>" + (ROOT / "web/static" / m[1]).read_text() + "</script>",
        html,
    )
    page.route(
        "http://app.test/**", lambda route: route.fulfill(content_type="text/html", body=html)
    )
    page.route(
        "**/api/screens/**",
        lambda route: route.fulfill(json={**screen, "points": screen.get("series") or []}),
    )
    data = map_payload()
    page.route("**/api/stocks/TEST/map", map_handler or (lambda route: route.fulfill(json=data)))
    old = filing_events(filing_row("old", filed_at="2026-08-01T18:00:00Z"))
    page.route(
        "**/api/stocks/TEST/map?cursor=*",
        lambda route: route.fulfill(
            json={**data, "events": old, "loaded_filings": 1, "next_cursor": None}
        ),
    )
    if connections_handler:
        page.route("**/map/connections?*", connections_handler)
    page.goto("http://app.test/stock/TEST", wait_until="domcontentloaded")
    if map_handler is None:
        expect(page.locator("[data-map-status]")).to_have_text("")
    return data


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_ticker_map_layout_keyboard_sources_and_shared_selection(page, width, tmp_path):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_map(page, width)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.locator("[data-map-graph]")).to_be_visible()
    expect(page.locator("[data-person]")).to_have_count(4 if width <= 500 else 11)
    expect(page.locator(".map-center-score")).to_have_text("45")
    expect(page.locator("[data-person][aria-pressed=true]")).to_have_count(0)
    expect(page.locator("[data-map-events] [aria-pressed=true]")).to_have_count(0)
    expect(page.locator("[data-map-events]")).to_be_visible()
    for row in page.locator("[data-map-events] .wallet-event").all():
        assert row.bounding_box()["height"] <= 48
    expect(
        page.locator('[data-map-events] [data-tone="buy"] .wallet-event-action').first
    ).to_have_css("color", "rgb(115, 206, 255)")
    expect(
        page.locator('[data-map-events] [data-tone="sell"] .wallet-event-action').first
    ).to_have_css("color", "rgb(239, 153, 164)")
    page.locator("[data-map-events]").screenshot(path=str(tmp_path / f"ticker-events-{width}.png"))
    expect(page.locator(".chart-filing-marker")).to_have_count(0)
    expect(page.locator("[data-map-score-return]")).to_be_hidden()
    bubble = page.locator("[data-person]").first
    name = bubble.get_attribute("aria-label").split(":")[0]
    bubble.focus()
    page.locator(".map-edge").first.dispatch_event("click")
    page.locator("[data-person]").first.focus()
    expect(page.locator("[data-person]").first).to_be_focused()
    expect(page.locator("[data-map-selection] h3")).to_have_text(name)
    expect(page.locator("[data-map-events] [aria-pressed=true]")).to_have_count(1)
    expect(page.locator("[data-map-selection] a")).to_have_attribute(
        "href", re.compile(r"^https://www\.sec\.gov/Archives/edgar/data/")
    )
    expect(page.locator(".chart-filing-marker")).to_have_count(1)
    if width <= 500:
        page.get_by_role("button", name="Next wallets").click()
        expect(page.locator("[data-person]")).to_have_count(4)
    else:
        expect(page.locator("[data-map-page]")).to_have_text("1–11 of 11 wallets")
        expect(page.locator("[data-map-paging]")).to_be_hidden()
    page.get_by_role("button", name="Next filings").click()
    expect(page.locator("[data-map-filings-page]")).to_have_text("6–10 of 11")
    page.locator("[data-map-events] button").first.click()
    expect(page.locator("[data-map-selection] h3")).not_to_have_text(re.compile(r"^\d+$"))
    assert not errors


def test_desktop_wallets_fill_one_orbit_before_paging(page, tmp_path):
    open_map(page, 1280)

    expect(page.locator("[data-person]")).to_have_count(11)
    expect(page.locator("[data-person].dense")).to_have_count(11)
    expect(page.locator("[data-map-page]")).to_have_text("1–11 of 11 wallets")
    expect(page.locator("[data-map-paging]")).to_be_hidden()

    positions = page.locator("[data-person]").evaluate_all(
        """nodes => nodes.map(node => {
            const circle = node.querySelector(':scope > circle');
            return [Number(circle.getAttribute('cx')), Number(circle.getAttribute('cy'))];
        })"""
    )
    assert len(set(map(tuple, positions))) == 11
    for x, y in positions:
        assert ((x - 380) / 270) ** 2 + ((y - 218) / 150) ** 2 == pytest.approx(1)
    assert min(x for x, _ in positions) < 120
    assert max(x for x, _ in positions) > 640
    assert min(y for _, y in positions) == pytest.approx(68)
    assert max(y for _, y in positions) > 360
    page.locator("[data-map-graph]").screenshot(path=str(tmp_path / "wallet-orbit.png"))


def _oldest_filing(page: Page) -> None:
    for _ in range(3):
        button = page.get_by_role("button", name="Next filings")
        if button.is_disabled():
            break
        button.click()


def test_clicking_a_filing_scrubs_the_map(page):
    """The list is the scrubber: clicking a filing moves the map to the
    people and events known by that filing's date, and the ring stays put."""

    open_map(page)
    geometry = page.locator(".map-score-segment").evaluate_all(
        "segments => segments.map(segment => segment.getAttribute('d'))"
    )
    expect(page.locator("[data-person]")).to_have_count(11)
    _oldest_filing(page)
    expect(page.locator("[data-map-filings-page]")).to_have_text("11–11 of 11")
    oldest = page.locator("[data-map-events] button").last
    oldest.click()
    expect(oldest).to_have_attribute("aria-pressed", "true")
    expect(page.locator("[data-person]")).to_have_count(1)
    expect(page.locator("[data-map-page]")).to_have_text("1–1 of 1 wallets")
    expect(page.locator("[data-map-paging]")).to_be_hidden()
    expect(page.locator("[data-map-selection] h3")).not_to_have_text(re.compile(r"^\d+$"))
    expect(page.locator(".chart-filing-marker")).to_have_count(1)
    assert (
        page.locator(".map-score-segment").evaluate_all(
            "segments => segments.map(segment => segment.getAttribute('d'))"
        )
        == geometry
    )

    page.get_by_role("button", name="Load older filings").click()
    expect(page.locator("[data-map-filings-page]")).to_have_text("11–12 of 12")
    expect(page.locator("[data-person]")).to_have_count(1)

    page.get_by_role("button", name="Return to score overview").click()
    expect(page.locator(".map-center-score")).to_have_text("45")
    expect(page.locator("[data-map-events] [aria-pressed=true]")).to_have_count(0)
    expect(page.locator("[data-person][aria-pressed=true]")).to_have_count(0)


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_map_uses_the_shared_three_slice_contract_and_no_summary_card(page, width):
    open_map(page, width)
    expect(page.locator(".stock-indicator-summary")).to_have_count(0)
    glyph = page.locator(".map-glyph")
    expect(glyph).to_have_attribute("data-band", "2")
    segments = page.locator(".map-score-segment")
    # Legacy negative social/penalty receipts cannot become attention wedges.
    expect(segments).to_have_count(2)
    expect(page.locator('[data-score-key="rug"], [data-score-key="social_search"]')).to_have_count(
        0
    )
    weights = [60, 40]
    radius = 48 if width <= 500 else 62
    lengths = [segment.evaluate("s => s.getTotalLength()") for segment in segments.all()]
    assert sum(lengths) == pytest.approx(math.tau * radius, rel=0.001)
    assert [n / sum(lengths) for n in lengths] == pytest.approx([0.6, 0.4], abs=0.001)
    for segment, weight in zip(segments.all(), weights, strict=True):
        expect(segment).to_have_attribute(
            "aria-label", re.compile(f"{weight}% of attention contributions")
        )
        expect(segment).to_have_attribute("role", "button")
        expect(segment).to_have_attribute("tabindex", "0")
    expect(page.locator(".map-score-penalties")).to_have_count(0)
    expect(page.locator(".map-center-score")).to_have_text("45")
    expect(page.get_by_text("Indicator key", exact=True)).to_have_count(1)
    assert page.locator(".indicator-legend").get_attribute("open") is None
    expect(page.locator(".map-glyph-reading")).to_contain_text(
        "Filing sentiment: bullish/bearish split unavailable"
    )


def ring_point(segment):
    segment.scroll_into_view_if_needed()
    return segment.evaluate("""segment => {
        const point = segment.getPointAtLength(segment.getTotalLength() / 2);
        const screen = point.matrixTransform(segment.getScreenCTM());
        return {x: screen.x, y: screen.y};
    }""")


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_selected_group_preserves_underlying_traces_and_refreshes(page, width):
    page.clock.install()
    current = score_current(
        score_trace={
            "market": [{"label": "Source", "value": "Activity inputs"}],
            "sec_event": [{"label": "Filing", "value": "S-3"}],
            "news": [{"label": "Articles", "value": "4"}],
        }
    )
    open_map(page, width, current=current)
    selection = page.locator("[data-map-selection]")
    expect(selection.locator(".map-score-trace")).to_have_count(0)
    page.locator('[data-score-key="evidence"]').press("Enter")
    expect(selection.locator(".map-score-trace")).to_have_count(2)
    expect(selection.locator(".map-score-trace dd")).to_have_text(["S-3", "4"])
    expect(selection.locator(".map-score-breakdown")).to_have_text(
        "+40 pts · 40% of attention contributions"
    )
    page.locator('[data-score-key="market"]').press("Space")
    screen = page.locator("#screenData").evaluate("node => JSON.parse(node.textContent)")
    screen["item"]["score_trace"]["market"][0]["value"] = "Refreshed activity inputs"
    page.route("**/api/screens/**", lambda route: route.fulfill(json=screen))
    with page.expect_response("**/api/screens/**"):
        page.clock.fast_forward(60000)
    expect(selection.locator(".map-score-trace")).to_contain_text("Refreshed activity inputs")
    expect(page.locator('[data-score-key="market"]')).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Return to score overview").click()
    expect(selection.locator(".map-score-trace")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("key", ["market", "evidence", "sentiment", "risk"])
def test_components_and_metadata_share_filing_selection_without_mixing_units(page, width, key):
    open_map(page, width, current=score_current(rug_score=68, sentiment="positive"))
    page.locator(".map-edge").first.dispatch_event("click")
    selection = page.locator("[data-map-selection]")
    title = selection.locator("h3").text_content()
    control = page.locator(f'[data-score-key="{key}"]')
    control.dispatch_event("mouseenter")
    expect(selection.locator("h4")).to_be_visible()
    control.dispatch_event("mouseleave")
    expect(selection.locator("h3")).to_have_text(title)
    control.press("Enter")
    expect(control).to_have_attribute("aria-pressed", "true")
    expect(page.locator("[data-event-id][aria-pressed=true]")).to_have_count(0)
    expect(page.locator(".chart-filing-marker")).to_have_count(0)
    if key == "risk":
        expect(selection.locator(".map-risk-reading")).to_contain_text("High structural risk")
        expect(selection.locator(".map-score-breakdown")).to_have_count(0)
    if key == "sentiment":
        expect(selection.locator(".map-sentiment-reading")).to_contain_text(
            "100% bullish, 0% bearish"
        )
        expect(selection.locator(".map-score-breakdown")).to_have_count(0)
    page.get_by_role("button", name="Next filings").click()
    expect(control).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("Escape")
    expect(control).to_have_attribute("aria-pressed", "false")
    expect(page.locator("[data-map-score-center]")).to_be_focused()


@pytest.mark.parametrize("response", ["empty", "error"])
def test_pending_empty_and_failed_filings_keep_attention_rendered(page, response):
    pending, errors = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    open_map(page, map_handler=lambda route: pending.append(route))
    expect(page.locator(".map-center-score")).to_have_text("45")
    expect(page.locator(".map-score-segment")).to_have_count(2)
    assert len(pending) == 1
    empty = {
        "ticker": "TEST",
        "events": [],
        "loaded_filings": 0,
        "coverage": {"filings": 0},
        "next_cursor": None,
    }
    if response == "error":
        pending.pop().fulfill(status=503, body="retry")
        expect(page.locator("[data-map-status]")).to_contain_text("Please retry")
        page.route("**/api/stocks/TEST/map", lambda route: route.fulfill(json=empty))
        page.get_by_role("button", name="Retry loading filings").click()
    else:
        pending.pop().fulfill(json=empty)
    expect(page.locator("[data-map-status]")).to_have_text("")
    expect(page.locator(".map-score-segment")).to_have_count(2)
    expect(page.locator("[data-person]")).to_have_count(0)
    expect(page.locator("[data-map-filings]")).to_be_hidden()
    assert errors == []


@pytest.mark.parametrize("score,mix", [(None, "unknown"), (0, "zero")])
def test_zero_and_missing_attention_remain_distinct_inside_map(page, score, mix):
    open_map(
        page,
        current=score_current(
            score=score, score_detail=None, score_components={"market": 0}, rug_score=80
        ),
    )
    expect(page.locator(".map-glyph")).to_have_attribute("data-mix", mix)
    expect(page.locator(".map-center-score")).to_have_text("—" if score is None else "0")
    expect(page.locator(".map-score-segment")).to_have_count(0)
    expect(page.locator(".map-score-track")).to_have_count(1)
    expect(page.locator(".map-glyph-risk")).to_have_attribute(
        "aria-label", "Risk: High. Show risk assessment."
    )
    expect(page.locator(".map-glyph-empty")).to_have_text(
        "No attention contributions." if score == 0 else "Attention breakdown unavailable."
    )
    expect(page.locator(".stock-indicator-summary")).to_have_count(0)
    expect(page.locator("[data-stock-map]")).not_to_contain_text(re.compile("NaN|Infinity"))


@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_keyboard_navigation_covers_slices_sentiment_and_risk(page, key):
    open_map(page)
    controls = page.locator(".map-glyph [data-score-key]")
    expect(controls).to_have_count(4)
    first = controls.first
    first.focus()
    first.press("ArrowRight")
    expect(controls.nth(1)).to_be_focused()
    controls.nth(1).press("End")
    expect(controls.last).to_be_focused()
    controls.last.press("ArrowDown")
    expect(first).to_be_focused()
    first.press("ArrowLeft")
    expect(controls.last).to_be_focused()
    controls.last.press("Home")
    expect(first).to_be_focused()
    first.press(key)
    _oldest_filing(page)
    page.locator("[data-map-events] button").last.click()
    expect(first).to_have_attribute("aria-pressed", "true")
    page.locator("[data-map-score-center]").press(key)
    expect(first).to_have_attribute("aria-pressed", "false")


@pytest.mark.parametrize("width", [320, 390])
@pytest.mark.parametrize("score", [24, 58, 80])
def test_touch_selects_ring_and_solid_slices(browser, width, score):
    context = browser.new_context(has_touch=True)
    try:
        page = context.new_page()
        open_map(page, width, current=score_current(score=score))
        segment = page.locator('[data-score-key="evidence"]')
        if score < 70:
            point = ring_point(segment)
        else:
            # Interior of the evidence wedge, avoiding the risk overlay and separators.
            segment.scroll_into_view_if_needed()
            point = page.locator("[data-map-graph]").evaluate("""graph => {
                const point = new DOMPoint(180-30,184-20).matrixTransform(graph.getScreenCTM());
                return {x:point.x,y:point.y};
            }""")
        page.touchscreen.tap(**point)
        expect(segment).to_have_attribute("aria-pressed", "true")
        page.locator("[data-map-score-center]").tap()
        expect(segment).to_have_attribute("aria-pressed", "false")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_attention_bands_have_two_sizes_and_true_solid_geometry(page, width, tmp_path):
    radii = []
    for score in [24, 58, 80]:
        open_map(
            page, width, current=score_current(score=score, rug_score=68, sentiment="positive")
        )
        radii.append(float(page.locator(".map-glyph-sentiment").get_attribute("r")))
        expect(page.locator(".map-glyph")).to_have_attribute(
            "data-band", "1" if score < 40 else "2" if score < 70 else "3"
        )
        expect(page.locator('[data-sentiment-side="bullish"]')).to_have_css(
            "stroke", "rgb(165, 229, 185)"
        )
        expect(page.locator(".map-risk-dot")).to_have_css("fill", "rgb(239, 153, 164)")
        if score >= 70:
            expect(page.locator(".map-glyph-hole")).to_have_count(0)
            for segment in page.locator(".map-score-segment").all():
                assert segment.get_attribute("d").endswith(" Z")
                assert segment.evaluate("s => getComputedStyle(s).fill") != "none"
                assert segment.evaluate("s => getComputedStyle(s).pointerEvents") == "fill"
            page.locator("[data-map-graph]").screenshot(
                path=str(tmp_path / f"map-solid-{width}.png")
            )
    assert radii[0] < radii[1] == radii[2]


@pytest.mark.parametrize(
    "risk,expected", [(10, "low"), (30, "medium"), (68, "high"), (None, "unknown")]
)
def test_map_center_uses_risk_marker_not_a_penalty_slice(page, risk, expected):
    open_map(page, current=score_current(rug_score=risk))
    expect(page.locator(".map-glyph")).to_have_attribute("data-risk", expected)
    expect(page.locator(".map-risk-dot")).to_have_count(0 if expected == "low" else 1)
    expect(page.locator(".map-risk-unknown")).to_have_count(int(expected == "unknown"))
    expect(page.locator(".map-score-segment")).to_have_count(2)


@pytest.mark.parametrize("score", [20, 80])
def test_single_slice_uses_full_circle_and_cap_does_not_distort_mix(page, score):
    open_map(
        page, current=score_current(score=score, score_components={"market": 80, "community": 99})
    )
    segment = page.locator("circle.map-score-segment")
    expect(segment).to_have_count(1)
    expect(segment).to_have_attribute("aria-label", re.compile("100% of attention contributions"))
    if score >= 70:
        expect(segment).not_to_have_css("fill", "none")
    open_map(
        page,
        current=score_current(
            score=100,
            score_components={"market": 80, "sec_event": 12, "news": 6, "social_search": 8},
        ),
    )
    expect(page.locator(".map-score-segment")).to_have_count(3)
    expect(page.locator('[data-score-key="market"]')).to_have_attribute(
        "aria-label", re.compile("75.5% of attention contributions")
    )


@pytest.mark.parametrize("width", [390, 1280])
def test_live_refresh_preserves_wallets_selection_and_updates_metadata_only(page, width):
    from runner_web.stock_indicator import stock_indicator

    page.clock.install()
    source = score_current(score=58, rug_score=30, sentiment="positive")
    open_map(page, width, current=source)
    screen = page.locator("#screenData").evaluate("node => JSON.parse(node.textContent)")
    page.route("**/api/screens/**", lambda route: route.fulfill(json=screen))

    def poll():
        screen["item"]["indicator"] = stock_indicator(source)
        with page.expect_response("**/api/screens/**"):
            page.clock.fast_forward(60000)

    page.get_by_role("button", name="Next filings").click()
    if width <= 500:
        page.get_by_role("button", name="Next wallets").click()
    page.locator(".map-edge").first.dispatch_event("click")
    wallet_page = page.locator("[data-map-page]").text_content()
    filing_page = page.locator("[data-map-filings-page]").text_content()
    title = page.locator("[data-map-selection] h3").text_content()
    page.evaluate("window.savedPerson = document.querySelector('[data-person]')")
    source.update(sentiment="risk", rug_score=68)  # Same attention and mix; metadata must refresh.
    poll()
    expect(page.locator(".map-glyph")).to_have_attribute("data-sentiment", "negative")
    expect(page.locator(".map-glyph")).to_have_attribute("data-risk", "high")
    expect(page.locator("[data-map-selection] h3")).to_have_text(title)
    assert page.evaluate("window.savedPerson === document.querySelector('[data-person]')")
    risk = page.locator('[data-score-key="risk"]')
    risk.press("Enter")
    source.update(rug_score=35)
    poll()
    expect(risk).to_be_focused()
    expect(risk).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".map-risk-reading")).to_contain_text("Medium structural risk")
    source.update(rug_score=0)
    poll()
    expect(risk).to_have_count(0)
    expect(page.locator("[data-map-score-center]")).to_be_focused()
    expect(page.locator("[data-map-page]")).to_have_text(wallet_page)
    expect(page.locator("[data-map-filings-page]")).to_have_text(filing_page)
    page.locator('[data-score-key="evidence"]').press("Enter")
    source.update(score=80)
    poll()
    expect(page.locator(".map-glyph")).to_have_attribute("data-band", "3")
    expect(page.locator('[data-score-key="evidence"]')).to_be_focused()
    page.evaluate("window.savedSegment = document.querySelector('.map-score-segment')")
    screen["item"]["value"] = "$999.00"
    poll()
    expect(page.locator("[data-value]")).to_have_text("$999.00")
    assert page.evaluate("window.savedSegment === document.querySelector('.map-score-segment')")
    wrong = {
        **screen,
        "item": {
            **screen["item"],
            "id": "OTHER",
            "indicator": stock_indicator(score_current(score=5)),
        },
    }
    page.evaluate(
        """next => document.getElementById('screenData').dispatchEvent(
            new CustomEvent('rati:screen-detail',{detail:next}))""",
        wrong,
    )
    expect(page.locator(".map-center-score")).to_have_text("80")
    source.update(score=None, score_detail=None, score_components={})
    poll()
    expect(page.locator(".map-score-segment")).to_have_count(0)
    expect(page.locator("[data-map-score-center]")).to_be_focused()
    expect(page.locator(".map-center-score")).to_have_text("—")
    expect(page.locator("[data-stock-map]")).not_to_contain_text(re.compile("NaN|Infinity"))


def _record_phases(page: Page) -> None:
    page.evaluate("""() => {
        window.mapPhases = [];
        const graph = document.querySelector('[data-map-graph]');
        new MutationObserver(() => window.mapPhases.push(graph.dataset.phase))
            .observe(graph, {attributes: true, attributeFilter: ['data-phase']});
    }""")


def test_scrubbing_a_filing_animates_the_people(page):
    open_map(page)
    page.emulate_media(reduced_motion="no-preference")
    _record_phases(page)
    graph = page.locator("[data-map-graph]")
    expect(graph).to_have_attribute("data-phase", "settled")
    _oldest_filing(page)
    page.locator("[data-map-events] button").last.click()
    expect(graph).to_have_attribute("data-phase", "settled")
    assert "moving" in page.evaluate("window.mapPhases")
    expect(page.locator("[data-person]")).to_have_count(1)


@pytest.mark.parametrize("width", [390, 1280])
def test_reduced_motion_scrubs_without_animation(page, width):
    open_map(page, width)
    _record_phases(page)
    graph = page.locator("[data-map-graph]")
    _oldest_filing(page)
    page.locator("[data-map-events] button").last.click()
    expect(graph).to_have_attribute("data-phase", "settled")
    assert "moving" not in page.evaluate("window.mapPhases")


def _orbit_position(page) -> dict[str, float]:
    return page.evaluate(
        r"""() => {
          const node = document.querySelector('[data-person]');
          const [x, y] = node.dataset.orbitAnchor.split(',').map(Number);
          const [dx, dy] = node.getAttribute('transform')
            .match(/translate\(([-\d.]+) ([-\d.]+)\)/).slice(1).map(Number);
          const graph = document.querySelector('[data-map-graph]');
          const [cx, cy] = graph.dataset.orbitCenter.split(',').map(Number);
          const [rx, ry] = graph.dataset.orbitTrack.split(',').map(Number);
          return {x: x + dx, y: y + dy, cx, cy, rx, ry};
        }"""
    )


def test_map_travels_along_its_ring_and_pauses_on_click(page):
    open_map(page)
    page.emulate_media(reduced_motion="no-preference")
    node = page.locator("[data-person]").first
    # Nodes move by translation alone, so the label can never turn upside down.
    expect(node).to_have_attribute("transform", re.compile(r"^translate\("))
    first = _orbit_position(page)

    def on_ring(point: dict[str, float]) -> float:
        return ((point["x"] - point["cx"]) / point["rx"]) ** 2 + (
            (point["y"] - point["cy"]) / point["ry"]
        ) ** 2

    # The node sits on the fixed ellipse track at every step, so the ring never swings.
    assert on_ring(first) == pytest.approx(1.0, abs=0.01)
    first_transform = node.get_attribute("transform")
    page.wait_for_timeout(1500)
    assert node.get_attribute("transform") != first_transform
    second = _orbit_position(page)
    assert on_ring(second) == pytest.approx(1.0, abs=0.01)
    assert (second["x"], second["y"]) != (first["x"], first["y"])
    graph = page.locator("[data-map-graph]")
    graph.dispatch_event("click")
    expect(graph).to_have_class(re.compile(r"\borbit-paused\b"))
    paused = node.get_attribute("transform")
    page.wait_for_timeout(1200)
    assert node.get_attribute("transform") == paused


def test_state_legend_tabs_filter_the_chart(page):
    """The status legend sits muted below the chart, never on it: its tabs
    carry the chart's status colours and toggle which run the line emphasises."""

    history = [{"time": f"2026-09-{day:02}T18:00:00Z", "price": day + 2} for day in range(14, 20)]
    states = [
        {"time": "2026-09-14T00:00:00Z", "tone": "watch"},
        {"time": "2026-09-16T06:00:00Z", "tone": "running"},
    ]
    open_map(page, current=score_current(trade_state="TRIGGERED"), states=states, history=history)

    legend = page.locator("[data-chart-state-legend]")
    expect(legend).to_be_visible()
    chips = legend.locator("button")
    expect(chips).to_have_count(2)
    expect(chips).to_have_text(["Running", "Watch"])
    expect(chips.first).to_have_attribute("aria-pressed", "false")
    expect(page.locator(".chart-state-label")).to_have_count(0)

    chips.first.click()
    expect(chips.first).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".chart-state.state-running")).not_to_have_css("opacity", "0.15")
    expect(page.locator(".chart-state.state-watch")).to_have_css("opacity", "0.15")

    chips.first.click()
    expect(chips.first).to_have_attribute("aria-pressed", "false")
    expect(page.locator(".chart-state.state-watch")).not_to_have_css("opacity", "0.15")
    expect(page.locator(".chart-state-label")).to_have_count(0)


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_inline_interests_link_to_stocks_and_keep_one_chart(page, width, tmp_path):
    data = map_payload()
    base = data["events"][0]
    rows = [
        {**base, "id": "large", "ticker": "BIG", "view": "ownership", "percent": 20},
        {**base, "id": "small", "ticker": "SMALL", "view": "ownership", "percent": 2},
        {**base, "id": "sale", "ticker": "BIG", "action": "Sold", "value": 100000},
        {**base, "id": "buy", "ticker": "SMALL", "action": "Bought", "value": 10000},
        {**base, "id": "self", "ticker": "TEST", "action": "Bought", "value": 1},
    ]

    def respond(route):
        route.fulfill(json={"person_id": "sec:101", "events": rows, "next_cursor": None})

    open_map(
        page,
        width,
        map_handler=lambda route: route.fulfill(json={**data, "events": [base]}),
        connections_handler=respond,
    )
    links = page.locator("[data-interest]")
    expect(links).to_have_count(2)
    expect(page.locator("[data-stock-map] svg")).to_have_count(1)
    expect(page.locator("[data-map-connections], [data-stock-map] select")).to_have_count(0)
    expect(page.locator('[data-interest][href="/stock/BIG"] .map-interest-edge')).to_have_count(2)
    expect(page.locator('[data-interest][href="/stock/SMALL"] .map-interest-edge')).to_have_count(2)
    expect(page.locator(".map-interest-edge.sell")).to_have_count(1)
    expect(page.locator(".map-interest-edge.buy")).to_have_count(3)
    big = page.locator('[data-interest][href="/stock/BIG"] .map-interest-dot')
    small = page.locator('[data-interest][href="/stock/SMALL"] .map-interest-dot')
    assert float(big.get_attribute("r")) > float(small.get_attribute("r"))
    assert float(big.get_attribute("r")) <= 6
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator("[data-map-graph]").screenshot(path=str(tmp_path / f"inline-map-{width}.png"))
    sale = page.locator('[data-interest][href="/stock/BIG"]')
    expect(sale).to_have_attribute("href", "/stock/BIG")
    page.route(
        "http://app.test/stock/BIG",
        lambda route: route.fulfill(body="<h1>BIG</h1>", content_type="text/html"),
    )
    sale.focus()
    sale.press("Enter")
    expect(page).to_have_url("http://app.test/stock/BIG")
    expect(page.get_by_role("heading", name="BIG")).to_be_visible()


@pytest.mark.parametrize("width", [390, 1280])
def test_attention_badges_exclude_penalties_and_filing_notes_are_visible(page, width, tmp_path):
    current = score_current(
        score_detail={
            "drivers": [{"key": "market", "label": "Scan", "value": 51.9}],
            "penalties": [{"key": "rug", "label": "Rug", "value": -6}],
        }
    )
    open_map(page, current=current, width=width)
    expect(page.locator(".map-center-score")).to_have_text("45")
    expect(page.locator("[data-map-selection] h3")).to_have_count(0)
    expect(page.locator(".map-score-legend li")).to_have_count(1)
    expect(page.locator(".map-score-legend li")).to_contain_text("Market")
    expect(page.locator(".map-score-legend li")).to_contain_text("+51.9 pts")
    expect(page.locator(".map-score-penalties")).to_have_count(0)
    expect(page.locator(".map-glyph-reading")).to_contain_text("Risk:")
    source = page.locator(".map-source")
    assert source.bounding_box()["height"] < 100
    assert source.evaluate("el => getComputedStyle(el).backgroundColor") == "rgba(0, 0, 0, 0)"
    page.locator(".map-workspace").screenshot(path=str(tmp_path / f"compact-tags-{width}.png"))
    data = map_payload()
    data["events"] = [data["events"][0]]
    data["events"][0]["footnotes"] = "Held through the family trust."
    page.route("**/api/stocks/TEST/map", lambda route: route.fulfill(json=data))
    page.reload()
    page.locator(".map-edge").first.dispatch_event("click")
    expect(page.locator(".map-filing-notes")).to_have_text("Held through the family trust.")
    expect(page.locator("[data-map-selection] details")).to_have_count(0)


def test_inline_interests_load_next_page_and_follow_filing_time(page):
    data = map_payload()
    base = data["events"][0]
    later = {**base, "id": "later", "ticker": "LATER", "filed_at": "2026-10-01T18:00:00Z"}
    old = {**base, "id": "old-interest", "ticker": "OLDER"}

    def respond(route):
        more = "cursor=" in route.request.url
        route.fulfill(
            json={
                "person_id": "sec:101",
                "events": [old] if more else [later],
                "next_cursor": None if more else "older",
            }
        )

    open_map(
        page,
        map_handler=lambda route: route.fulfill(json={**data, "events": [base]}),
        connections_handler=respond,
    )
    expect(page.locator('[data-interest][href="/stock/OLDER"]')).to_have_count(1)
    expect(page.locator('[data-interest][href="/stock/LATER"]')).to_have_count(1)
    page.locator("[data-map-events] button").first.click()
    expect(page.locator('[data-interest][href="/stock/LATER"]')).to_have_count(0)
    expect(page.locator('[data-interest][href="/stock/OLDER"]')).to_have_count(1)


def test_wallet_opens_portfolio_and_repeated_events_share_one_bubble(page):
    data = map_payload()
    base = data["events"][0]
    data["events"] = [base, {**base, "id": "second", "action": "Sold", "tone": "down"}]
    open_map(page, map_handler=lambda route: route.fulfill(json=data))
    expect(page.locator("[data-person]")).to_have_count(1)
    expect(page.locator(".map-edge")).to_have_count(2)
    paths = page.locator(".map-edge").evaluate_all("els => els.map(el => el.getAttribute('d'))")
    assert len(set(paths)) == 2
    wallet = page.locator("[data-person]")
    href = wallet.get_attribute("href")
    assert href.startswith("/wallet/w_")
    page.route(f"http://app.test{href}", lambda route: route.fulfill(body="Wallet portfolio"))
    wallet.focus()
    wallet.press("Enter")
    expect(page).to_have_url(f"http://app.test{href}")


@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("has_holdings", [True, False])
@pytest.mark.parametrize("score", [24, 45, 80, 0, None])
def test_wallet_page_shares_main_stock_rows_and_shows_filing_history(
    page, width, tmp_path, has_holdings, score
):
    from runner_web.entity_view import entity_view
    from runner_web.market_screens import listing

    request = _request()
    rows = [{**score_current(score=score), "ticker": ticker} for ticker in ["USO", "CDTG"]]
    rows[0].update(
        rug_score=68, sentiment="positive", sentiment_counts={"bullish": 3, "bearish": 1}
    )
    rows[1].update(score=80, score_detail=None)
    events = [
        {
            **row,
            "ticker": "USO" if i < 2 else "CDTG",
            "post_shares": 100 - i * 10 if has_holdings else None,
            "people": [
                {"id": "sec:101", "name": "HRT FINANCIAL LP", "role": "Investor"},
                {"id": "sec:202", "name": "COATUE MANAGEMENT LLC", "role": "Investor"},
            ],
        }
        for i, row in enumerate(map_payload()["events"][:3])
    ]
    html = main.templates.TemplateResponse(
        request,
        "stock_wallet.html",
        main.page_context(
            request,
            None,
            resolved_user=None,
            screen=listing("stocks", rows),
            wallet={"name": "HRT FINANCIAL LP", "id": "sec:101"},
            wallet_events=events,
            wallet_cursor=None,
            wallet_ticker="USO",
            entity=entity_view(events, rows, "sec:101"),
        ),
    ).body.decode()
    html = re.sub(
        r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
        lambda match: "<style>" + (ROOT / "web/static" / match[1]).read_text() + "</style>",
        html,
    )
    html = re.sub(
        r'<script src="/static/([^\"?]+)[^"]*"[^>]*></script>',
        lambda match: "<script>" + (ROOT / "web/static" / match.group(1)).read_text() + "</script>",
        html,
    )
    page.route(
        "http://app.test/**", lambda route: route.fulfill(content_type="text/html", body=html)
    )
    # The second hop comes from the stock's own holder list, not this wallet's
    # filings.
    page.route(
        "**/api/stocks/*/map",
        lambda route: route.fulfill(
            json={
                "ticker": "USO",
                "events": [
                    {
                        "action": "Bought",
                        "view": "market",
                        "people": [{"id": "sec:202", "name": "COATUE MANAGEMENT LLC"}],
                    }
                ],
                "next_cursor": None,
            }
        ),
    )
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto("http://app.test/wallets/stocks/USO/sec:101")
    expect(page.get_by_role("heading", name="HRT FINANCIAL LP")).to_be_visible()
    expect(page.locator(".ticker-list .ticker")).to_have_count(2)
    expect(page.locator(".ticker-score")).to_have_count(2)
    expect(page.locator(".wallet-event")).to_have_count(3)
    for row in page.locator(".wallet-event").all():
        assert row.bounding_box()["height"] <= 48
    expect(page.locator('[data-tone="buy"] .wallet-event-action')).to_have_css(
        "color", "rgb(115, 206, 255)"
    )
    expect(page.locator('[data-tone="sell"] .wallet-event-action')).to_have_css(
        "color", "rgb(239, 153, 164)"
    )
    expect(page.locator(".wallet-filing").first).to_have_attribute(
        "href", re.compile(r"^https://www\.sec\.gov/")
    )
    expect(page.locator(".entity-center")).to_have_attribute("aria-label", "HRT FINANCIAL LP")
    expect(page.locator("[data-entity-stock]")).to_have_count(2)
    # A silent script failure is how an empty map hides; say so instead.
    assert errors == [], errors
    # Both phone and desktop orbit with upright labels.
    stock = page.locator("[data-entity-stock]").first
    expect(stock).to_have_attribute("transform", re.compile(r"^translate\("))
    point = page.evaluate(
        r"""() => {
          const node = document.querySelector('[data-entity-stock]');
          const [x, y] = node.dataset.orbitAnchor.split(',').map(Number);
          const [dx, dy] = node.getAttribute('transform')
            .match(/translate\(([-\d.]+) ([-\d.]+)\)/).slice(1).map(Number);
          const graph = document.querySelector('[data-entity-map]');
          const [cx, cy] = graph.dataset.orbitCenter.split(',').map(Number);
          const [rx, ry] = graph.dataset.orbitTrack.split(',').map(Number);
          return {onRing: ((x + dx - cx) / rx) ** 2 + ((y + dy - cy) / ry) ** 2};
        }"""
    )
    assert point["onRing"] == pytest.approx(1.0, abs=0.01)
    expect(page.locator("[data-entity-paging]")).to_have_count(0)
    # Second hop: the other wallets that reported the same stocks. The dot's
    # colour carries the signal, so there is no text label to crowd the map.
    expect(page.locator("[data-entity-interest]")).to_have_count(2)
    expect(page.locator(".map-interest-name")).to_have_count(0)
    expect(page.locator("[data-entity-interest]").first).to_have_class(re.compile(r"\bbuy\b"))
    wheels = page.locator(".entity-score-ring")
    expect(wheels).to_have_count(2)
    wheel = page.locator('[data-entity-stock="USO"] .entity-score-ring')
    expect(wheel.locator(".map-score-segment")).to_have_count(2)
    expect(page.locator('[data-entity-stock="CDTG"] .map-score-track')).to_have_count(1)
    expect(page.locator('[data-entity-stock="CDTG"] .map-score-segment')).to_have_count(0)
    # The map and list share stock contributions, fill, tone and risk.
    for ticker in ["USO", "CDTG"]:
        link = page.locator(f'[data-entity-stock="{ticker}"]')
        row_glyph = page.locator(f'.ticker[href="/stock/{ticker}"] .indicator-glyph')
        face = link.locator(".map-glyph")
        for name in ["band", "sentiment", "sentiment-mix", "risk", "mix"]:
            expect(face).to_have_attribute("data-" + name, row_glyph.get_attribute("data-" + name))
        assert row_glyph.get_attribute("aria-label") in link.get_attribute("aria-label")
        expect(link.locator('[role="button"], [tabindex]')).to_have_count(0)
        expect(link.locator(".entity-stock-symbol")).to_have_text(ticker)
        value = link.locator(".map-node-action").text_content()
        if has_holdings:
            assert value.startswith("$")
        else:
            assert value == ("2 events" if ticker == "USO" else "1 event")
    # The larger holding gets the larger ring, even at a lower attention score.
    radii = wheels.locator(".map-glyph-sentiment").evaluate_all(
        "nodes => nodes.map(node => Number(node.getAttribute('r')))"
    )
    # The largest holding is first in the map and in keyboard order.
    assert radii[0] > radii[1] if has_holdings else radii[0] == radii[1]
    page.get_by_text("Entity map key", exact=True).click()
    expect(
        page.get_by_text("Ring size follows the reported holding value", exact=False)
    ).to_be_visible()
    expect(wheel.locator(".map-risk-dot")).to_have_css("fill", "rgb(239, 153, 164)")
    expect(wheel.locator('[data-sentiment-side="bullish"]')).to_have_css(
        "stroke", "rgb(165, 229, 185)"
    )
    expect(wheel.locator('[data-sentiment-side="bullish"]')).to_have_attribute("data-share", "0.75")
    expect(wheel.locator('[data-sentiment-side="bearish"]')).to_have_attribute("data-share", "0.25")
    expect(wheel.locator('[data-score-key="rug"]')).to_have_count(0)
    unknown = page.locator('[data-entity-stock="CDTG"]')
    expect(unknown.locator(".map-risk-unknown")).to_have_text("?")
    expect(unknown.locator(".map-score-track")).to_have_css("stroke-dasharray", "4px, 4px")
    for key, color in [("market", "rgb(65, 140, 244)"), ("evidence", "rgb(181, 138, 244)")]:
        segment = wheel.locator(f'[data-score-key="{key}"]')
        expect(segment).to_have_css("fill" if score == 80 else "stroke", color)
    if score == 80:
        expect(wheel.locator(".map-glyph-hole")).to_have_count(0)
        assert all(
            segment.get_attribute("d").endswith(" Z")
            for segment in wheel.locator(".map-score-segment").all()
        )
    else:
        lengths = wheel.locator(".map-score-segment").evaluate_all(
            "parts => parts.map(part => part.getTotalLength())"
        )
        assert [length / sum(lengths) for length in lengths] == pytest.approx([0.6, 0.4], abs=0.001)
    # Labels remain outside the face, including the solid high-attention shape.
    for link in page.locator("[data-entity-stock]").all():
        face_box = link.locator(".map-glyph-sentiment").bounding_box()
        label_box = link.locator(".entity-stock-symbol").bounding_box()
        assert label_box["y"] >= face_box["y"] + face_box["height"]
    expect(page.locator(".entity-edge")).to_have_count(3)
    if has_holdings:
        expect(page.locator(".entity-worth-chart")).to_be_visible()
        expect(page.locator(".entity-worth-point")).to_have_count(3)
    else:
        expect(page.locator(".entity-worth-chart")).to_be_hidden()
        expect(page.locator("[data-worth-pending]")).to_be_visible()
        expect(page.locator(".entity-worth-value")).to_have_text("—")
    expect(page.locator('[data-entity-stock="USO"]')).to_have_attribute("href", "/stock/USO")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"wallet-{width}.png"), full_page=True)
    page.locator(".wallet-events").screenshot(path=str(tmp_path / f"events-{width}.png"))
    link = page.locator('[data-entity-stock="USO"]')
    link.focus()
    link.press("Enter")
    expect(page).to_have_url("http://app.test/stock/USO")


def test_wallet_events_load_more_as_the_tail_comes_into_view(page):
    """Filings arrive by scrolling, not by hunting for a next page."""
    from runner_web.entity_view import entity_view
    from runner_web.market_screens import listing

    request = _request()
    rows = [{**score_current(score=45), "ticker": "USO"}]
    holder = [{"id": "sec:101", "name": "HRT FINANCIAL LP", "role": "Investor"}]
    first = [{**map_payload()["events"][0], "ticker": "USO", "post_shares": 10, "people": holder}]
    later = [{**map_payload()["events"][1], "ticker": "USO", "post_shares": 5, "people": holder}]
    html = main.templates.TemplateResponse(
        request,
        "stock_wallet.html",
        main.page_context(
            request,
            None,
            resolved_user=None,
            screen=listing("stocks", rows),
            wallet={"name": "HRT FINANCIAL LP", "id": "sec:101"},
            wallet_events=first,
            wallet_cursor="page-2",
            wallet_id="w_testwallet",
            wallet_ticker="USO",
            entity=entity_view(first, rows, "sec:101"),
        ),
    ).body.decode()
    html = re.sub(
        r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
        lambda match: "<style>" + (ROOT / "web/static" / match[1]).read_text() + "</style>",
        html,
    )
    html = re.sub(
        r'<script src="/static/(map-orbit|ring-glyph|entity-map-navigation|'
        r"entity-map|wallet-events)"
        r'\.js[^\"]*"[^>]*></script>',
        lambda match: (
            "<script>" + (ROOT / "web/static" / f"{match.group(1)}.js").read_text() + "</script>"
        ),
        html,
    )
    page.route(
        "http://app.test/**", lambda route: route.fulfill(content_type="text/html", body=html)
    )
    partial = main.templates.get_template("_wallet_event.html")
    rendered = "".join(partial.render(event=event, wallet={"id": "sec:101"}) for event in later)
    page.route(
        re.compile(r"/api/wallets/.*/filings"),
        lambda route: route.fulfill(json={"html": rendered, "next_cursor": None, "count": 1}),
    )
    page.set_viewport_size({"width": 1280, "height": 700})
    page.goto("http://app.test/wallets/stocks/USO/sec:101")

    expect(page.locator(".wallet-event")).to_have_count(1)
    # Scrolling the tail into view is what loads the next page.
    page.locator("[data-wallet-more]").scroll_into_view_if_needed()
    expect(page.locator(".wallet-event")).to_have_count(2)
    expect(page.locator("[data-wallet-more]")).to_have_count(0)


def test_source_failure_can_retry_and_reported_names_are_text(page):
    open_map(page, map_handler=lambda route: route.fulfill(status=503, body="retry"))
    page.reload()
    expect(page.get_by_role("button", name="Retry loading filings")).to_be_visible()
    payload = map_payload()
    payload["events"][-1]["people"][0]["name"] = "<img src=x onerror=alert(1)>"
    page.route("**/api/stocks/TEST/map", lambda route: route.fulfill(json=payload))
    page.get_by_role("button", name="Retry loading filings").click()
    page.locator(f'[data-edge-event="{payload["events"][-1]["id"]}"]').dispatch_event("click")
    expect(page.locator("[data-map-selection] h3")).to_have_text("<img src=x onerror=alert(1)>")
    expect(page.locator("[data-map-selection] img")).to_have_count(0)


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_sentiment_ratio_refresh_preserves_focus_and_clears_to_dashed(page, width):
    from runner_web.stock_indicator import stock_indicator

    page.clock.install()
    source = score_current(sentiment="positive", sentiment_counts={"bullish": 3, "bearish": 1})
    open_map(page, width, current=source)
    glyph = page.locator(".map-glyph")
    control = glyph.locator('[data-score-key="sentiment"]')
    control.press("Enter")
    screen = page.locator("#screenData").evaluate("node => JSON.parse(node.textContent)")
    page.route("**/api/screens/**", lambda route: route.fulfill(json=screen))
    for bullish, bearish in [(3, 1), (1, 3), (0, 0)]:
        source["sentiment_counts"] = {"bullish": bullish, "bearish": bearish}
        screen["item"]["indicator"] = stock_indicator(source)
        with page.expect_response("**/api/screens/**"):
            page.clock.fast_forward(60000)
        expect(control).to_be_focused()
        expect(control).to_have_attribute("aria-pressed", "true")
        if bullish + bearish:
            share = bullish / (bullish + bearish)
            green = glyph.locator('[data-sentiment-side="bullish"]')
            red = glyph.locator('[data-sentiment-side="bearish"]')
            expect(green).to_have_attribute(
                "stroke-dasharray", f"{share * 100:g} {100 - share * 100:g}"
            )
            expect(red).to_have_attribute("stroke-dashoffset", f"{-share * 100:g}")
            expect(green).to_have_css("stroke", "rgb(165, 229, 185)")
            expect(red).to_have_css("stroke", "rgb(239, 153, 164)")
            expect(control).to_have_attribute("aria-label", re.compile(f"{share:.0%} bullish"))
        else:
            expect(glyph.locator(".map-sentiment-part")).to_have_count(0)
            expect(control).to_have_css("stroke-dasharray", "7px, 6px")
            expect(page.locator(".map-sentiment-reading")).to_contain_text("split unavailable")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
