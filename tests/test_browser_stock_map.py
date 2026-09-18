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


def open_map(page: Page, width=1280, *, current=None, map_handler=None, states=None, history=None):
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
    page.goto("http://app.test/t/TEST", wait_until="domcontentloaded")
    if map_handler is None:
        expect(page.locator("[data-map-status]")).to_have_text("")
    return data


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_ticker_map_layout_keyboard_sources_and_shared_selection(page, width):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    open_map(page, width)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.locator("[data-map-graph]")).to_be_visible()
    expect(page.locator("[data-person]")).to_have_count(4 if width <= 500 else 8)
    expect(page.locator("[data-map-selection] h3")).to_have_text("45")
    expect(page.locator("[data-person][aria-pressed=true]")).to_have_count(0)
    expect(page.locator("[data-map-events] [aria-pressed=true]")).to_have_count(0)
    expect(page.locator("[data-map-events]")).to_be_visible()
    expect(page.locator(".chart-filing-marker")).to_have_count(0)
    expect(page.locator("[data-map-score-return]")).to_be_hidden()
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
    page.get_by_role("button", name="Next filings").click()
    expect(page.locator("[data-map-filings-page]")).to_have_text("6–10 of 11")
    page.locator("[data-map-events] button").first.click()
    expect(page.locator("[data-map-selection] h3")).not_to_have_text(re.compile(r"^\d+$"))
    assert not errors


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
    expect(page.locator("[data-person]")).to_have_count(8)
    _oldest_filing(page)
    expect(page.locator("[data-map-filings-page]")).to_have_text("11–11 of 11")
    oldest = page.locator("[data-map-events] button").last
    oldest.click()
    expect(oldest).to_have_attribute("aria-pressed", "true")
    expect(page.locator("[data-person]")).to_have_count(1)
    expect(page.locator("[data-map-page]")).to_have_text("1–1 of 1 people")
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
    expect(page.locator("[data-map-selection] h3")).to_have_text("45")
    expect(page.locator("[data-map-events] [aria-pressed=true]")).to_have_count(0)
    expect(page.locator("[data-person][aria-pressed=true]")).to_have_count(0)


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_scoring_panel_segments_share_the_ring_by_total_magnitude(page, width):
    open_map(page, width)
    segments = page.locator(".map-score-segment")
    expect(segments).to_have_count(5)
    expect(page.locator(".map-score-penalty-arc, [data-penalty-key]")).to_have_count(0)
    radius = 48 if width <= 500 else 62
    cx, cy = (180, 184) if width <= 500 else (380, 218)
    angle = -math.pi / 2
    lengths = []
    weights = [60, 30, 10, -50, -5]
    total = sum(abs(weight) for weight in weights)
    for segment, weight in zip(segments.all(), weights, strict=True):
        d = segment.get_attribute("d")
        values = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?(?:e[+-]?\d+)?", d)]
        end = angle + abs(weight) / total * math.tau
        assert values == pytest.approx(
            [
                cx + radius * math.cos(angle),
                cy + radius * math.sin(angle),
                radius,
                radius,
                0,
                int(abs(weight) > total / 2),
                1,
                cx + radius * math.cos(end),
                cy + radius * math.sin(end),
            ]
        )
        lengths.append(segment.evaluate("segment => segment.getTotalLength()"))
        percent = round(abs(weight) / total * 100, 1)
        label = f"{weight:+} pts, {percent}% of total magnitude"
        expect(segment).to_have_attribute("aria-label", re.compile(re.escape(label)))
        expect(segment.locator("title")).to_contain_text(label.replace(", ", " · "))
        expect(segment).to_have_attribute("stroke-width", "16" if width <= 500 else "20")
        expect(segment).to_have_attribute("role", "button")
        expect(segment).to_have_attribute("tabindex", "0")
        assert segment.evaluate("el => getComputedStyle(el).pointerEvents") == "stroke"
        if weight < 0:
            assert segment.evaluate("el => getComputedStyle(el).stroke") == "rgb(239, 153, 164)"
        angle = end
    assert angle == pytest.approx(3 * math.pi / 2)
    assert sum(lengths) == pytest.approx(math.tau * radius, rel=0.001)
    assert [length / sum(lengths) for length in lengths] == pytest.approx(
        [abs(weight) / total for weight in weights], abs=0.001
    )
    expect(page.locator(".map-center-score")).to_have_text("45")


def ring_point(segment):
    segment.scroll_into_view_if_needed()
    return segment.evaluate("""segment => {
        const point = segment.getPointAtLength(segment.getTotalLength() / 2);
        const screen = point.matrixTransform(segment.getScreenCTM());
        return {x: screen.x, y: screen.y};
    }""")


def test_score_panel_pins_and_summarizes_penalties(page):
    open_map(page)
    selection = page.locator("[data-map-selection]")
    expect(selection.locator("h3")).to_have_text("45")
    expect(selection.locator(".map-score-legend")).to_have_count(0)
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
        "+60 pts · 38.7% of total magnitude"
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


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize(
    "key, label, breakdown",
    [
        ("rug", "Rug risk", "-50 pts · 32.3% of total magnitude"),
        ("social_search", "Social / search", "-5 pts · 3.2% of total magnitude"),
    ],
)
def test_risk_hover_and_pin_share_filing_selection(page, width, key, label, breakdown):
    open_map(page, width)
    page.locator("[data-person]").first.press("Enter")
    selection = page.locator("[data-map-selection]")
    filing_title = selection.locator("h3").text_content()
    risk = page.locator(f'[data-score-key="{key}"]')
    page.mouse.move(**ring_point(risk))
    expect(selection.locator("h4")).to_have_text(label)
    expect(page.locator(".map-score-breakdown")).to_have_text(breakdown)
    expect(risk).to_have_attribute("aria-pressed", "false")
    page.mouse.move(0, 0)
    expect(selection.locator("h3")).to_have_text(filing_title)
    expect(page.locator(".chart-filing-marker")).to_have_count(1)
    risk.focus()
    expect(selection.locator("h4")).to_have_text(label)
    page.mouse.click(**ring_point(risk))
    page.mouse.move(0, 0)
    page.get_by_role("button", name="Next people").focus()
    expect(risk).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".map-score-breakdown")).to_have_text(breakdown)
    expect(selection).to_contain_text("Pinned contribution")
    expect(page.locator("[data-person][aria-pressed=true]")).to_have_count(0)
    expect(page.locator("[data-event-id][aria-pressed=true]")).to_have_count(0)
    expect(page.locator(".chart-filing-marker")).to_have_count(0)
    page.get_by_role("button", name="Next people").click()
    expect(page.locator(".map-score-breakdown")).to_have_text(breakdown)
    page.get_by_role("button", name="Next filings").click()
    expect(page.locator("[data-map-filings-page]")).to_have_text("6–10 of 11")
    expect(risk).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".map-score-breakdown")).to_have_text(breakdown)
    page.get_by_role("button", name="Return to score overview").click()
    expect(page.locator("[data-map-score-center]")).to_be_focused()
    expect(page.locator("[data-map-filings-page]")).to_have_text("6–10 of 11")
    expect(risk).to_have_attribute("aria-pressed", "false")
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
    expect(page.locator(".map-score-segment")).to_have_count(5)
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
        expect(page.locator("[data-map-status]")).to_have_text("")
    expect(page.locator("[data-map-selection] h3")).to_have_text("45")
    expect(page.locator(".map-score-segment")).to_have_count(5)
    expect(page.locator("[data-person]")).to_have_count(0)
    expect(page.locator("[data-map-events] .map-note")).to_have_text(
        "Saved filings will appear here."
    )
    if response == "error":
        page.route("**/api/stocks/TEST/map", lambda route: route.fulfill(json=map_payload()))
        page.get_by_role("button", name="Retry loading filings").click()
        expect(page.locator("[data-map-status]")).to_have_text("")
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
@pytest.mark.parametrize(
    "score_key, breakdown",
    [
        ("market", "+60 pts · 38.7% of total magnitude"),
        ("rug", "-50 pts · 32.3% of total magnitude"),
        ("social_search", "-5 pts · 3.2% of total magnitude"),
    ],
)
def test_score_keyboard_navigation_and_pin_survive_filing_scrub(page, key, score_key, breakdown):
    open_map(page)
    segments = page.locator(".map-score-segment")
    segments.first.focus()
    segments.first.press("ArrowRight")
    expect(segments.nth(1)).to_be_focused()
    expect(page.locator(".map-score-breakdown")).to_have_text(
        "+30 pts · 19.4% of total magnitude"
    )
    segments.nth(1).press("End")
    expect(segments.last).to_be_focused()
    segments.last.press("ArrowDown")
    expect(segments.first).to_be_focused()
    segments.first.press("ArrowLeft")
    expect(segments.last).to_be_focused()
    segments.last.press("ArrowUp")
    expect(page.locator('[data-score-key="rug"]')).to_be_focused()
    expect(page.locator(".map-score-breakdown")).to_have_text(
        "-50 pts · 32.3% of total magnitude"
    )
    page.keyboard.press("Home")
    expect(segments.first).to_be_focused()
    segment = page.locator(f'[data-score-key="{score_key}"]')
    segment.press(key)
    expect(segment).to_be_focused()
    expect(segment).to_have_attribute("aria-pressed", "true")
    _oldest_filing(page)
    page.locator("[data-map-events] button").last.click()
    expect(segment).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".map-score-segment[aria-pressed=true]")).to_have_count(1)
    page.get_by_role("button", name="Load older filings").click()
    expect(page.locator("[data-map-events] button")).to_have_count(2)
    expect(segment).to_have_attribute("aria-pressed", "true")
    page.locator("[data-map-score-center]").press(key)
    expect(page.locator("[data-map-score-center]")).to_be_focused()
    expect(segment).to_have_attribute("aria-pressed", "false")
    expect(page.locator(".map-score-breakdown")).to_have_count(0)


@pytest.mark.parametrize("width", [320, 390])
@pytest.mark.parametrize(
    "key, breakdown",
    [
        ("sec_event", "+30 pts · 19.4% of total magnitude"),
        ("rug", "-50 pts · 32.3% of total magnitude"),
    ],
)
def test_touch_pins_ring_and_center_returns_to_score(browser, width, key, breakdown):
    context = browser.new_context(has_touch=True)
    try:
        page = context.new_page()
        open_map(page, width)
        segment = page.locator(f'[data-score-key="{key}"]')
        page.touchscreen.tap(**ring_point(segment))
        expect(segment).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".map-score-breakdown")).to_have_text(breakdown)
        expect(page.locator("[data-map-selection]")).to_contain_text("Pinned contribution")
        page.locator("[data-map-score-center]").tap()
        expect(page.locator(".map-score-segment[aria-pressed=true]")).to_have_count(0)
        expect(page.locator("[data-map-score-return]")).to_be_hidden()
        expect(page.locator("[data-map-selection] h3")).to_have_text("45")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        context.close()


@pytest.mark.parametrize("weight, penalty_value", [(0, -60), (60, -60), (60, 60), (60, 0)])
def test_single_contribution_and_mixed_rings_have_valid_geometry(page, weight, penalty_value):
    current = score_current(
        score=0,
        score_detail={
            "drivers": [{"key": "market", "label": "Market scanner", "value": weight}],
            "penalties": [{"key": "rug", "label": "Rug risk", "value": penalty_value}],
        },
    )
    open_map(page, current=current)
    expect(page.locator(".map-center-score")).to_have_text("0")
    segments = page.locator(".map-score-segment")
    mixed = bool(weight and penalty_value)
    expect(segments).to_have_count(2 if mixed else 1)
    expect(page.locator(".map-score-penalty-arc")).to_have_count(0)
    if mixed:
        expect(page.locator("circle.map-score-segment")).to_have_count(0)
        for segment in segments.all():
            assert segment.evaluate("s => s.getTotalLength()") == pytest.approx(
                math.pi * 62, rel=0.001
            )
    else:
        expect(segments).to_have_attribute("r", "62")
        assert segments.evaluate("s => s.getTotalLength()") == pytest.approx(
            math.tau * 62, rel=0.01
        )
    if weight:
        page.locator('[data-score-key="market"]').press("Enter")
        expect(page.locator(".map-score-breakdown")).to_have_text(
            f"+60 pts · {50 if mixed else 100}% of total magnitude"
        )
    else:
        expect(page.locator("[data-map-selection]")).to_contain_text("No positive contributions.")
    if penalty_value:
        expect(page.locator(".map-score-penalties strong")).to_have_text("-60 pts")
        penalty = page.locator('[data-score-key="rug"]')
        assert penalty.evaluate("el => getComputedStyle(el).stroke") == "rgb(239, 153, 164)"
        expect(penalty).to_have_attribute("stroke-width", "20")
        penalty.press("Enter")
        expect(penalty).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".map-score-breakdown")).to_have_text(
            f"-60 pts · {50 if mixed else 100}% of total magnitude"
        )
    else:
        expect(page.locator('[data-score-key="rug"]')).to_have_count(0)
        expect(page.locator("[data-map-selection]")).to_contain_text("No penalties applied.")
    expect(page.locator("[data-stock-map]")).not_to_contain_text(re.compile("NaN|Infinity"))


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("key, removal", [("rug", "zero"), ("social_search", "remove")])
def test_polling_preserves_risk_pin_and_clears_missing_risk(page, width, key, removal):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.clock.install()
    open_map(page, width)
    screen = page.locator("#screenData").evaluate("node => JSON.parse(node.textContent)")
    page.route("**/api/screens/**", lambda route: route.fulfill(json=screen))

    def poll(score):
        screen["item"]["score"] = score
        with page.expect_response("**/api/screens/**"):
            page.clock.fast_forward(60000)
        expect(page.locator(".map-center-score")).to_have_text(str(score))

    page.get_by_role("button", name="Next filings").click()
    page.get_by_role("button", name="Next people").click()
    people_page = page.locator("[data-map-page]").text_content()
    filings_page = page.locator("[data-map-filings-page]").text_content()
    risk = page.locator(f'[data-score-key="{key}"]')
    risk.press("Space")
    page.get_by_role("button", name="Previous people").focus()
    detail = screen["item"]["score_detail"]
    parts = detail["penalties"] if key == "rug" else detail["drivers"]
    part = next(part for part in parts if part["key"] == key)
    part["value"] = -20
    detail["drivers"].reverse()
    poll(25)
    total = 125 if key == "rug" else 170
    percent = "16" if key == "rug" else "11.8"
    breakdown = f"-20 pts · {percent}% of total magnitude"
    expect(risk).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".map-score-breakdown")).to_have_text(breakdown)
    expect(page.locator("[data-map-selection]")).to_contain_text("Pinned contribution")
    expect(page.get_by_role("button", name="Previous people")).to_be_focused()
    lengths = page.locator(".map-score-segment").evaluate_all(
        "segments => segments.map(segment => segment.getTotalLength())"
    )
    assert risk.evaluate("el => el.getTotalLength()") / sum(lengths) == pytest.approx(
        20 / total, abs=0.001
    )
    risk.focus()
    part["value"] = -10
    poll(30)
    expect(risk).to_be_focused()
    expect(risk).to_have_attribute("aria-pressed", "true")
    percent = "8.7" if key == "rug" else "6.3"
    expect(page.locator(".map-score-breakdown")).to_have_text(
        f"-10 pts · {percent}% of total magnitude"
    )
    assert risk.evaluate("el => getComputedStyle(el).stroke") == "rgb(239, 153, 164)"
    if removal == "zero":
        part["value"] = 0
    else:
        parts.remove(part)
    poll(40)
    expect(risk).to_have_count(0)
    expect(page.locator("[data-map-score-center]")).to_be_focused()
    expect(page.locator(".map-score-breakdown")).to_have_count(0)
    expect(page.locator("[data-map-score-return]")).to_be_hidden()
    expect(page.locator("[data-map-page]")).to_have_text(people_page)
    expect(page.locator("[data-map-filings-page]")).to_have_text(filings_page)
    expect(page.locator(".map-score-penalty-arc")).to_have_count(0)
    assert not errors


@pytest.mark.parametrize("width", [390, 1280])
def test_polling_refreshes_score_without_resetting_filing_or_pinned_state(page, width):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.clock.install()
    initial_time = "2026-09-13T19:00:00+00:00"
    open_map(page, width, current=score_current(score_as_of=initial_time))
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
    page.get_by_role("button", name="Next filings").click()
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
    expect(page.locator("[data-map-load]")).to_be_hidden()
    social = page.locator('[data-score-key="social_search"]')
    market = page.locator('[data-score-key="market"]')
    assert social.evaluate("el => getComputedStyle(el).stroke") == "rgb(255, 173, 112)"
    assert market.evaluate("el => getComputedStyle(el).stroke") == "rgb(115, 206, 255)"
    lengths = page.locator(".map-score-segment").evaluate_all(
        "segments => segments.map(segment => segment.getTotalLength())"
    )
    assert [length / sum(lengths) for length in lengths] == pytest.approx(
        [2 / 7, 4 / 7, 1 / 7], abs=0.001
    )
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
        "+20 pts · 44.4% of total magnitude"
    )
    expect(page.locator("[data-map-selection]")).to_contain_text("Pinned contribution")
    expect(page.locator("[data-map-selection] h3")).to_have_text("35")
    expect(page.locator(".map-score-penalties strong")).to_have_text("-5 pts")
    penalty = page.locator('[data-score-key="rug"]')
    expect(penalty).to_have_count(1)
    expect(page.locator(".map-score-penalty-arc")).to_have_count(0)
    assert penalty.evaluate("el => getComputedStyle(el).stroke") == "rgb(239, 153, 164)"
    expect(page.locator("[data-map-page]")).to_have_text(pagination)
    page.evaluate("window.savedSegment = document.querySelector('[data-score-key]')")
    screen["item"]["value"] = "$999.00"
    poll()
    expect(page.locator("[data-value]")).to_have_text("$999.00")
    assert page.evaluate("window.savedSegment === document.querySelector('[data-score-key]')")
    expect(social).to_be_focused()
    screen["item"]["score_as_of"] = "2026-09-16T22:00:00+00:00"
    poll()
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
    expect(page.locator("[data-map-score-return]")).to_be_hidden()
    screen["item"].update(score=0, score_detail={"drivers": [None], "penalties": None})
    poll()
    expect(page.locator(".map-center-score")).to_have_text("0")
    expect(page.locator("[data-map-selection]")).to_contain_text("No positive contributions.")
    expect(page.locator("[data-stock-map]")).not_to_contain_text(re.compile("NaN|Infinity"))
    assert not errors


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



def test_state_legend_tabs_filter_the_chart(page):
    """The map's state legend replaces the score driver list: the chips carry
    the chart's status colours and toggle which run the line emphasises."""

    history = [
        {"time": f"2026-09-{day:02}T18:00:00Z", "price": day + 2} for day in range(14, 20)
    ]
    states = [
        {"time": "2026-09-14T00:00:00Z", "tone": "watch"},
        {"time": "2026-09-16T06:00:00Z", "tone": "running"},
    ]
    open_map(page, current=score_current(trade_state="TRIGGERED"), states=states, history=history)

    chips = page.locator("[data-map-state-legend] .chip")
    expect(chips).to_have_count(2)
    expect(chips).to_have_text(["Running", "Watch"])
    expect(chips.first).to_have_attribute("aria-pressed", "false")
    expect(page.locator(".chart-state-label.state-watch")).to_have_text("WATCH")
    expect(page.locator(".chart-state-label.state-running")).to_have_text("RUNNING")

    chips.first.click()
    expect(chips.first).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".chart-state.state-running")).not_to_have_css("opacity", "0.15")
    expect(page.locator(".chart-state.state-watch")).to_have_css("opacity", "0.15")
    expect(page.locator(".chart-state-label.state-watch")).to_have_count(0)

    chips.first.click()
    expect(chips.first).to_have_attribute("aria-pressed", "false")
    expect(page.locator(".chart-state.state-watch")).not_to_have_css("opacity", "0.15")
    expect(page.locator(".chart-state-label.state-watch")).to_have_text("WATCH")
