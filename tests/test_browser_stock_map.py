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


def open_map(page: Page, width=1280, *, current=None, map_handler=None):
    page.set_viewport_size({"width": width, "height": 900})
    request = _request()
    detail = {
        "ticker": "TEST",
        "company": "Test Company",
        "current": score_current() if current is None else current,
        "events": [],
    }
    screen = main.simple_market_detail("stocks", detail)
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
    page.route("**/api/screens/**", lambda route: route.fulfill(json=screen))
    data = map_payload()
    page.route("**/api/stocks/TEST/map", map_handler or (lambda route: route.fulfill(json=data)))
    old = filing_events(filing_row("old", filed_at="2026-08-01T18:00:00Z"))
    page.route(
        "**/api/stocks/TEST/map?cursor=*",
        lambda route: route.fulfill(
            json={**data, "events": old, "loaded_filings": 1, "next_cursor": None}
        ),
    )
    page.goto("http://app.test/t/TEST", wait_until="domcontentloaded")
    if map_handler is None:
        expect(page.locator("[data-map-status]")).to_contain_text("Saved SEC filings")
    return data


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_ticker_map_layout_keyboard_sources_and_shared_selection(page, width):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_map(page, width)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.get_by_role("heading", name="TEST map", exact=True)).to_be_visible()
    expect(page.locator("[data-person]")).to_have_count(4 if width <= 500 else 8)
    expect(page.locator("[data-map-selection] h3")).to_have_text("45")
    expect(page.locator("[data-person][aria-pressed=true]")).to_have_count(0)
    expect(page.locator("[data-map-events] [aria-pressed=true]")).to_have_count(0)
    expect(page.locator(".chart-filing-marker")).to_have_count(0)
    expect(page.locator("[data-map-score-return]")).to_be_hidden()
    expect(page.locator(".map-filings")).not_to_have_attribute("open", "")
    expect(page.locator("[data-map-events]")).to_be_hidden()
    bubble = page.locator("[data-person]").first
    name = bubble.get_attribute("aria-label").split(":")[0]
    bubble.focus()
    bubble.press("Enter")
    expect(page.locator("[data-person]").first).to_be_focused()
    expect(page.locator("[data-map-selection] h3")).to_have_text(name)
    expect(page.locator("[data-map-events] [aria-pressed=true]")).to_have_count(1)
    expect(page.locator("[data-map-selection] a")).to_have_attribute(
        "href", re.compile(r"^https://www\.sec\.gov/Archives/edgar/data/")
    )
    expect(page.locator(".chart-filing-marker")).to_have_count(1)
    page.get_by_role("button", name="Next people").click()
    expect(page.locator("[data-person]")).to_have_count(4 if width <= 500 else 3)
    page.locator("[data-map-list-title]").click()
    page.get_by_role("combobox", name="Filter reported action").select_option("Sold")
    expect(page.locator("[data-map-events] button")).to_have_count(3)
    expect(page.locator("[data-map-events]")).to_be_visible()
    page.locator("[data-map-events] button").first.click()
    expect(page.locator("[data-map-selection] h3")).not_to_have_text(re.compile(r"^\d+$"))
    page.locator("[data-map-list-title]").press("Enter")
    expect(page.locator("[data-map-events]")).to_be_hidden()
    expect(page.locator("[data-map-selection] h3")).not_to_have_text(re.compile(r"^\d+$"))
    page.locator("[data-map-list-title]").press("Space")
    expect(page.locator("[data-map-events]")).to_be_visible()
    expect(page.locator("[data-map-events] [aria-pressed=true]")).to_have_count(1)
    assert not errors


def test_filing_time_excludes_later_disclosures_and_preserves_loaded_history(page):
    open_map(page)
    geometry = page.locator(".map-score-segment").evaluate_all(
        "segments => segments.map(segment => segment.getAttribute('d'))"
    )
    timestamp = page.locator("[data-map-score-time]").text_content()
    page.locator("[data-person]").first.press("Enter")
    expect(page.locator("[data-map-selection] h3")).not_to_have_text(re.compile(r"^\d+$"))
    slider = page.get_by_role("slider", name="Filings known by")
    slider.focus()
    slider.press("Home")
    expect(page.locator("[data-map-events] button")).to_have_count(1)
    expect(page.locator("[data-map-selection] h3")).to_have_text("45")
    expect(page.locator("[data-map-events] [aria-pressed=true]")).to_have_count(0)
    expect(page.locator(".chart-filing-marker")).to_have_count(0)
    expect(page.locator(".map-center-score")).to_have_text("45")
    expect(page.locator("[data-map-score-time]")).to_have_text(timestamp)
    assert (
        page.locator(".map-score-segment").evaluate_all(
            "segments => segments.map(segment => segment.getAttribute('d'))"
        )
        == geometry
    )
    page.get_by_role("button", name="Load older filings").click()
    expect(page.locator("[data-map-coverage]")).to_contain_text("12 filings")
    expect(page.locator("[data-map-events] button")).to_have_count(2)
    expect(page.locator("[data-map-time]")).to_have_text("Sep 1, 2026")
    page.get_by_role("button", name="Latest", exact=True).click()
    expect(page.locator("[data-map-events] button")).to_have_count(12)
    expect(page.get_by_role("button", name="Load older filings")).to_be_hidden()
    expect(page.locator(".map-center-score")).to_have_text("45")
    expect(page.locator("[data-map-score-time]")).to_have_text(timestamp)
    assert (
        page.locator(".map-score-segment").evaluate_all(
            "segments => segments.map(segment => segment.getAttribute('d'))"
        )
        == geometry
    )

    page.locator("[data-person]").first.press("Enter")
    expect(page.locator("[data-map-selection] h3")).not_to_have_text(re.compile(r"^\d+$"))
    page.get_by_role("button", name="Return to score overview").click()
    expect(page.locator("[data-map-selection] h3")).to_have_text("45")
    expect(page.locator("[data-map-events] [aria-pressed=true]")).to_have_count(0)
    page.locator("[data-map-list-title]").click()
    page.locator("[data-map-events] button").first.click()
    expect(page.locator("[data-map-selection] h3")).not_to_have_text(re.compile(r"^\d+$"))
    expect(page.locator("[data-map-score-return]")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("[data-map-selection] h3")).to_have_text("45")
    page.locator("[data-map-list-title]").click()


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_scoring_panel_segments_share_the_ring_by_positive_contribution(page, width):
    open_map(page, width)
    segments = page.locator(".map-score-segment")
    expect(segments).to_have_count(3)
    radius = 48 if width <= 500 else 62
    cx, cy = (180, 184) if width <= 500 else (380, 218)
    angle = -math.pi / 2
    lengths = []
    for segment, weight in zip(segments.all(), [60, 30, 10], strict=True):
        d = segment.get_attribute("d")
        values = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?(?:e[+-]?\d+)?", d)]
        end = angle + weight / 100 * math.tau
        assert values == pytest.approx(
            [
                cx + radius * math.cos(angle),
                cy + radius * math.sin(angle),
                radius,
                radius,
                0,
                int(weight > 50),
                1,
                cx + radius * math.cos(end),
                cy + radius * math.sin(end),
            ]
        )
        lengths.append(segment.evaluate("segment => segment.getTotalLength()"))
        expect(segment).to_have_attribute(
            "aria-label", re.compile(rf"\+{weight} pts, {weight}% of positive contributions")
        )
        angle = end
    assert [length / sum(lengths) for length in lengths] == pytest.approx(
        [0.6, 0.3, 0.1], abs=0.001
    )
    expect(page.locator(".map-center-score")).to_have_text("45")


def ring_point(segment):
    segment.scroll_into_view_if_needed()
    return segment.evaluate("""segment => {
        const point = segment.getPointAtLength(segment.getTotalLength() / 2);
        const screen = point.matrixTransform(segment.getScreenCTM());
        return {x: screen.x, y: screen.y};
    }""")


def test_score_panel_pins_and_summarizes_positive_drivers_and_penalties(page):
    open_map(page)
    selection = page.locator("[data-map-selection]")
    expect(selection.locator("h3")).to_have_text("45")
    legend = selection.locator(".map-score-legend li")
    expect(legend).to_have_count(4)
    expect(legend.locator("strong")).to_have_text(["+60 pts", "+30 pts", "+10 pts", "0 pts"])
    expect(legend.locator("span:not([aria-hidden])")).to_have_text(
        ["Market scanner", "SEC events", "News", "Community"]
    )
    penalties = selection.locator(".map-score-penalties li")
    expect(penalties).to_have_count(2)
    expect(penalties.locator("span")).to_have_text(["Rug risk", "Social / search"])
    expect(penalties.locator("strong")).to_have_text(["-50 pts", "-5 pts"])
    first = page.locator(".map-score-segment").first
    page.mouse.move(**ring_point(first))
    expect(first).to_have_attribute("aria-pressed", "false")
    page.mouse.move(0, 0)
    first.focus()
    expect(first).to_have_attribute("aria-pressed", "false")
    expect(selection.locator("p.map-score-breakdown")).to_have_text(
        "+60 pts · 60% of positive contributions"
    )
    first.focus()
    page.mouse.click(**ring_point(first))
    expect(first).to_have_attribute("aria-pressed", "true")
    expect(selection.locator("p.map-note").first).to_contain_text("Pinned contribution")
    expect(page.locator("[data-map-score-return]")).to_be_visible()
    page.mouse.move(20, 20)
    page.locator("[data-map-score-return]").click()
    expect(selection.locator("h3")).to_have_text("45")
    expect(first).to_have_attribute("aria-pressed", "false")
    expect(page.locator("[data-map-score-return]")).to_be_hidden()
    page.locator(".map-score-segment").nth(1).focus()
    page.keyboard.press("Enter")
    expect(page.locator(".map-score-segment").nth(1)).to_have_attribute("aria-pressed", "true")
    page.keyboard.press("Escape")
    expect(page.locator(".map-score-segment").nth(1)).to_have_attribute("aria-pressed", "false")
    expect(selection.locator("h3")).to_have_text("45")


@pytest.mark.parametrize("response", ["empty", "error"])
def test_pending_empty_and_failed_filings_keep_score_rendered(page, response):
    pending = []
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    open_map(page, map_handler=lambda route: pending.append(route))
    expect(page.locator("[data-map-status]")).to_have_text("Loading saved SEC filings…")
    expect(page.locator("[data-map-selection] h3")).to_have_text("45")
    expect(page.locator(".map-center-score")).to_have_text("45")
    expect(page.locator(".map-score-segment")).to_have_count(3)
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
    else:
        pending.pop().fulfill(json=empty)
        expect(page.locator("[data-map-status]")).to_have_text("No filings yet")
    expect(page.locator("[data-map-selection] h3")).to_have_text("45")
    expect(page.locator(".map-score-segment")).to_have_count(3)
    expect(page.locator("[data-person]")).to_have_count(0)
    expect(page.locator("[data-map-time-slider]")).to_be_disabled()
    if response == "error":
        page.route("**/api/stocks/TEST/map", lambda route: route.fulfill(json=map_payload()))
        page.get_by_role("button", name="Retry loading filings").click()
        expect(page.locator("[data-map-status]")).to_have_text("Saved SEC filings")
        expect(page.locator("[data-map-selection] h3")).to_have_text("45")
        expect(page.locator("[data-person][aria-pressed=true]")).to_have_count(0)
    assert not errors


@pytest.mark.parametrize("score", [None, 0])
@pytest.mark.parametrize("breakdown", [None, {"drivers": [], "penalties": []}])
def test_missing_and_zero_score_are_graceful(page, score, breakdown):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    open_map(page, current=score_current(score=score, score_detail=breakdown))
    text = "—" if score is None else "0"
    expect(page.locator("[data-map-selection] h3")).to_have_text(text)
    expect(page.locator(".map-center-score")).to_have_text(text)
    expect(page.locator(".map-score-segment")).to_have_count(0)
    expect(page.locator(".map-score-track")).to_have_count(1)
    expect(page.locator("[data-map-selection]")).to_contain_text(
        "Score breakdown unavailable." if breakdown is None else "No positive contributions."
    )
    if score is None:
        expect(page.locator("[data-map-score-time]")).to_contain_text("Timestamp unavailable")
    else:
        expect(page.locator("[data-map-score-time] time")).to_have_attribute(
            "datetime", "2026-09-12T18:00:00+00:00"
        )
    expect(page.locator("[data-stock-map]")).not_to_contain_text(re.compile("NaN|Infinity"))
    page.locator("[data-person]").first.press("Enter")
    expect(page.locator("[data-map-selection] h3")).not_to_have_text(re.compile(r"^\d+$"))
    page.locator("[data-map-score-center]").press("Enter")
    expect(page.locator("[data-map-selection] h3")).to_have_text(text)
    assert not errors


def test_source_failure_can_retry_and_reported_names_are_text(page):
    open_map(page, map_handler=lambda route: route.fulfill(status=503, body="retry"))
    page.reload()
    expect(page.get_by_role("button", name="Retry loading filings")).to_be_visible()
    payload = map_payload()
    payload["events"][-1]["people"][0]["name"] = "<img src=x onerror=alert(1)>"
    page.route("**/api/stocks/TEST/map", lambda route: route.fulfill(json=payload))
    page.get_by_role("button", name="Retry loading filings").click()
    page.locator("[data-person]").filter(has_text="<img src=x onerror=alert(1)>").click()
    expect(page.locator("[data-map-selection] h3")).to_have_text("<img src=x onerror=alert(1)>")
    expect(page.locator("[data-map-selection] img")).to_have_count(0)


@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_score_keyboard_navigation_and_pin_survive_filing_scrub(page, key):
    open_map(page)
    segments = page.locator(".map-score-segment")
    segments.first.focus()
    segments.first.press("ArrowRight")
    expect(segments.nth(1)).to_be_focused()
    expect(page.locator(".map-score-breakdown")).to_have_text(
        "+30 pts · 30% of positive contributions"
    )
    segments.nth(1).press("End")
    expect(segments.last).to_be_focused()
    segments.last.press("ArrowDown")
    expect(segments.first).to_be_focused()
    segments.first.press("ArrowLeft")
    expect(segments.last).to_be_focused()
    segments.last.press("Home")
    segments.first.press(key)
    expect(segments.first).to_be_focused()
    expect(segments.first).to_have_attribute("aria-pressed", "true")
    page.get_by_role("slider", name="Filings known by").press("Home")
    expect(segments.first).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".map-score-breakdown")).to_have_text(
        "+60 pts · 60% of positive contributions"
    )
    page.get_by_role("button", name="Load older filings").focus()
    page.locator("[data-map-score-center]").press(key)
    expect(page.locator("[data-map-score-center]")).to_be_focused()
    expect(segments.first).to_have_attribute("aria-pressed", "false")
    expect(page.locator(".map-score-breakdown")).to_have_count(0)


@pytest.mark.parametrize("width", [320, 390])
def test_touch_pins_ring_and_center_returns_to_score(browser, width):
    context = browser.new_context(has_touch=True)
    try:
        page = context.new_page()
        open_map(page, width)
        segment = page.locator(".map-score-segment").nth(1)
        page.touchscreen.tap(**ring_point(segment))
        expect(segment).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".map-score-breakdown")).to_have_text(
            "+30 pts · 30% of positive contributions"
        )
        expect(page.locator("[data-map-selection]")).to_contain_text("Pinned contribution")
        page.locator("[data-map-score-center]").tap()
        expect(page.locator(".map-score-segment[aria-pressed=true]")).to_have_count(0)
        expect(page.locator("[data-map-score-return]")).to_be_hidden()
        expect(page.locator("[data-map-selection] h3")).to_have_text("45")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


@pytest.mark.parametrize("weight", [0, 60])
def test_zero_driver_or_single_positive_driver_has_valid_ring(page, weight):
    current = score_current(
        score=0,
        score_detail={
            "drivers": [{"key": "market", "label": "Market scanner", "value": weight}],
            "penalties": [{"key": "rug", "label": "Rug risk", "value": -60}],
        },
    )
    open_map(page, current=current)
    expect(page.locator(".map-center-score")).to_have_text("0")
    expect(page.locator(".map-score-penalties strong")).to_have_text("-60 pts")
    if weight:
        segment = page.locator("circle.map-score-segment")
        expect(segment).to_have_count(1)
        expect(segment).to_have_attribute("r", "62")
        assert segment.evaluate("s => s.getTotalLength()") == pytest.approx(math.tau * 62, rel=0.01)
        segment.press("Enter")
        expect(page.locator(".map-score-breakdown")).to_have_text(
            "+60 pts · 100% of positive contributions"
        )
    else:
        expect(page.locator(".map-score-segment")).to_have_count(0)
        expect(page.locator(".map-score-legend strong")).to_have_text("0 pts")
        expect(page.locator("[data-map-selection]")).to_contain_text("No positive contributions.")
    expect(page.locator("[data-stock-map]")).not_to_contain_text(re.compile("NaN|Infinity"))


@pytest.mark.parametrize("width", [390, 1280])
def test_polling_refreshes_score_without_resetting_filing_or_pinned_state(page, width):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.clock.install()
    initial_time = "2026-09-13T19:00:00+00:00"
    open_map(page, width, current=score_current(score_as_of=initial_time))
    expect(page.locator("[data-map-score-time] time")).to_have_attribute("datetime", initial_time)
    screen = page.locator("#screenData").evaluate("node => JSON.parse(node.textContent)")
    screen["series"] = [
        {"time": f"2026-09-{day:02}T18:00:00Z", "value": day + 2} for day in range(1, 12)
    ]
    page.route("**/api/screens/**", lambda route: route.fulfill(json=screen))
    page.evaluate("""() => {
        window.detailUpdates = 0;
        document.getElementById('screenData').addEventListener('rati:screen-detail', () => {
            window.detailUpdates++;
        });
    }""")

    def poll():
        updates = page.evaluate("window.detailUpdates")
        page.clock.fast_forward(60000)
        page.wait_for_function("count => window.detailUpdates > count", arg=updates)

    page.get_by_role("button", name="Load older filings").click()
    expect(page.locator("[data-map-coverage]")).to_contain_text("12 filings")
    slider = page.get_by_role("slider", name="Filings known by")
    slider.press("End")
    slider.press("ArrowLeft")
    cutoff = slider.input_value()
    filing_time = page.locator("[data-map-time]").text_content()
    page.get_by_role("button", name="Next people").click()
    pagination = page.locator("[data-map-page]").text_content()
    person = page.locator("[data-person]").first
    person.press("Enter")
    person_id = person.get_attribute("data-person")
    selected_event = page.locator("[data-event-id][aria-pressed=true]").get_attribute(
        "data-event-id"
    )
    filing_title = page.locator("[data-map-selection] h3").text_content()
    page.evaluate("window.savedPerson = document.querySelector('[data-person]')")
    screen["item"].update(
        score=50,
        score_as_of="2026-09-14T20:00:00+00:00",
        score_detail={
            "drivers": [
                {"key": "market", "label": "Market scanner", "value": 20},
                {"key": "social_search", "label": "Social / search", "value": 40},
            ],
            "penalties": [{"key": "rug", "label": "Rug risk", "value": -10}],
        },
    )
    poll()
    expect(page.locator(".map-center-score")).to_have_text("50")
    expect(page.locator("[data-map-selection] h3")).to_have_text(filing_title)
    expect(page.locator(f'[data-person="{person_id}"]')).to_be_focused()
    expect(page.locator(f'[data-event-id="{selected_event}"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator(".chart-filing-marker")).to_have_count(1)
    assert page.evaluate("window.savedPerson === document.querySelector('[data-person]')")
    expect(page.locator("[data-map-page]")).to_have_text(pagination)
    assert slider.input_value() == cutoff
    expect(page.locator("[data-map-time]")).to_have_text(filing_time)
    expect(page.locator("[data-map-load]")).to_be_hidden()
    social = page.locator('[data-score-key="social_search"]')
    market = page.locator('[data-score-key="market"]')
    assert social.evaluate("el => getComputedStyle(el).stroke") == "rgb(255, 173, 112)"
    assert market.evaluate("el => getComputedStyle(el).stroke") == "rgb(115, 206, 255)"
    lengths = page.locator(".map-score-segment").evaluate_all(
        "segments => segments.map(segment => segment.getTotalLength())"
    )
    assert [length / sum(lengths) for length in lengths] == pytest.approx([1 / 3, 2 / 3], abs=0.001)
    social.press("Enter")
    expect(social).to_have_attribute("aria-pressed", "true")
    screen["item"]["score_detail"]["drivers"].reverse()
    screen["item"]["score_detail"]["drivers"][0]["value"] = 20
    screen["item"]["score_detail"]["penalties"][0]["value"] = -5
    screen["item"].update(score=35, score_as_of="2026-09-15T21:00:00+00:00")
    poll()
    expect(social).to_be_focused()
    expect(social).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".map-score-breakdown")).to_have_text(
        "+20 pts · 50% of positive contributions"
    )
    expect(page.locator("[data-map-selection]")).to_contain_text("Pinned contribution")
    expect(page.locator("[data-map-selection] h3")).to_have_text("35")
    expect(page.locator(".map-score-legend strong")).to_have_text(["+20 pts", "+20 pts"])
    expect(page.locator(".map-score-penalties strong")).to_have_text("-5 pts")
    expect(page.locator("[data-map-score-time] time")).to_have_attribute(
        "datetime", screen["item"]["score_as_of"]
    )
    expect(page.locator("[data-map-page]")).to_have_text(pagination)
    assert slider.input_value() == cutoff
    page.evaluate("window.savedSegment = document.querySelector('[data-score-key]')")
    screen["item"]["value"] = "$999.00"
    poll()
    expect(page.locator("[data-value]")).to_have_text("$999.00")
    assert page.evaluate("window.savedSegment === document.querySelector('[data-score-key]')")
    expect(social).to_be_focused()
    screen["item"]["score_as_of"] = "2026-09-16T22:00:00+00:00"
    poll()
    expect(page.locator("[data-map-score-time] time")).to_have_attribute(
        "datetime", screen["item"]["score_as_of"]
    )
    assert page.evaluate("window.savedSegment === document.querySelector('[data-score-key]')")
    screen["item"].update(id="OTHER", score=99, value="$123.00")
    updates = page.evaluate("window.detailUpdates")
    with page.expect_response("**/api/screens/**"):
        page.clock.fast_forward(60000)
    page.clock.run_for(50)
    assert page.evaluate("window.detailUpdates") == updates
    page.evaluate(
        """next => document.getElementById('screenData').dispatchEvent(
        new CustomEvent('rati:screen-detail', {detail:next}))""",
        screen,
    )
    expect(page.locator(".map-center-score")).to_have_text("35")
    expect(page.locator("[data-value]")).to_have_text("$999.00")
    expect(social).to_be_focused()
    expect(social).to_have_attribute("aria-pressed", "true")
    screen["item"].update(id="TEST", score=None, score_detail=None, score_as_of=None)
    poll()
    expect(page.locator(".map-center-score")).to_have_text("—")
    expect(page.locator("[data-map-score-center]")).to_be_focused()
    expect(page.locator(".map-score-segment")).to_have_count(0)
    expect(page.locator("[data-map-selection]")).to_contain_text("Score breakdown unavailable.")
    expect(page.locator("[data-map-score-time]")).to_contain_text("Timestamp unavailable")
    expect(page.locator("[data-map-score-return]")).to_be_hidden()
    screen["item"].update(score=0, score_detail={"drivers": [None], "penalties": None})
    poll()
    expect(page.locator(".map-center-score")).to_have_text("0")
    expect(page.locator("[data-map-selection]")).to_contain_text("No positive contributions.")
    expect(page.locator("[data-stock-map]")).not_to_contain_text(re.compile("NaN|Infinity"))
    assert not errors
