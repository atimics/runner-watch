import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from runner_web import main
from runner_web.stock_map import filing_events
from tests.test_browser_ticker import _request
from tests.test_market_screens import sample
from tests.test_stock_map import evidence, filing_row

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


def open_map(page: Page, width=1280):
    page.set_viewport_size({"width": width, "height": 900})
    request = _request()
    detail = {
        "ticker": "TEST",
        "company": "Test Company",
        "current": sample("stocks"),
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
    page.route("**/api/stocks/TEST/map", lambda route: route.fulfill(json=data))
    old = filing_events(filing_row("old", filed_at="2026-08-01T18:00:00Z"))
    page.route(
        "**/api/stocks/TEST/map?cursor=*",
        lambda route: route.fulfill(
            json={**data, "events": old, "loaded_filings": 1, "next_cursor": None}
        ),
    )
    page.goto("http://app.test/t/TEST")
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
    page.get_by_role("combobox", name="Filter reported action").select_option("Sold")
    expect(page.locator("[data-map-events] button")).to_have_count(3)
    assert not errors


def test_filing_time_excludes_later_disclosures_and_preserves_loaded_history(page):
    open_map(page)
    slider = page.get_by_role("slider", name="Known by filing date")
    slider.focus()
    slider.press("Home")
    expect(page.locator("[data-map-events] button")).to_have_count(1)
    expect(page.locator("[data-map-selection] h3")).to_have_text("Person 0")
    page.get_by_role("button", name="Load older filings").click()
    expect(page.locator("[data-map-coverage]")).to_contain_text("12 filings")
    expect(page.locator("[data-map-events] button")).to_have_count(2)
    expect(page.locator("[data-map-time]")).to_have_text("Sep 1, 2026")
    page.get_by_role("button", name="Latest", exact=True).click()
    expect(page.locator("[data-map-events] button")).to_have_count(11)
    expect(page.get_by_role("button", name="Load older filings")).to_be_hidden()


def test_source_failure_can_retry_and_reported_names_are_text(page):
    open_map(page)
    page.route("**/api/stocks/TEST/map", lambda route: route.fulfill(status=503, body="retry"))
    page.reload()
    expect(page.get_by_role("button", name="Retry loading filings")).to_be_visible()
    payload = map_payload()
    payload["events"][-1]["people"][0]["name"] = "<img src=x onerror=alert(1)>"
    page.route("**/api/stocks/TEST/map", lambda route: route.fulfill(json=payload))
    page.get_by_role("button", name="Retry loading filings").click()
    page.locator("[data-person]").filter(has_text="<img src=x onerror=alert(1)>").click()
    expect(page.locator("[data-map-selection] h3")).to_have_text("<img src=x onerror=alert(1)>")
    expect(page.locator("[data-map-selection] img")).to_have_count(0)
