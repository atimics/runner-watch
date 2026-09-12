from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from runner_web.market_screens import detail, listing

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "screen_fixtures", ROOT / "tests/test_market_screens.py"
)
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
pytestmark = pytest.mark.browser


def open_screen(page, screen, user=None):
    html = fixtures.render(screen, user)
    html = re.sub(
        r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
        lambda m: "<style>" + (ROOT / "web/static" / m[1]).read_text() + "</style>",
        html,
    )
    html = re.sub(
        r'<script src="/static/market-screen.js[^\"]*"[^>]*></script>',
        lambda _: "<script>" + (ROOT / "web/static/market-screen.js").read_text() + "</script>",
        html,
    )
    page.route("http://app.test/", lambda r: r.fulfill(content_type="text/html", body=html))
    page.route("**/api/screens/**", lambda r: r.fulfill(json={"points": []}))
    page.goto("http://app.test/")


@pytest.mark.parametrize("market", ["stocks", "memecoins", "sports"])
@pytest.mark.parametrize("width", [390, 1280])
def test_list_layout_and_navigation_are_shared(page: Page, market, width):
    page.set_viewport_size({"width": width, "height": 844})
    open_screen(page, listing(market, [fixtures.sample(market)]))
    expect(page.get_by_role("navigation", name="Market").get_by_role("link")).to_have_count(3)
    expect(page.get_by_role("navigation", name="View").get_by_role("link")).to_have_count(2)
    expect(page.get_by_role("heading", name="List", exact=True)).to_be_visible()
    expect(page.locator(".ticker")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".ticker").bounding_box()["y"] < 450
    expect(page.get_by_text(fixtures.SENTINEL)).to_have_count(0)


@pytest.mark.parametrize("market", ["stocks", "memecoins", "sports"])
def test_map_items_are_clickable(page: Page, market):
    screen = listing(market, [fixtures.sample(market)], view="map")
    open_screen(page, screen)
    href = screen["rows"][0]["href"]
    page.route("http://app.test" + href, lambda r: r.fulfill(body="Detail opened"))
    page.get_by_role("link", name="Open " + screen["rows"][0]["name"], exact=True).click()
    expect(page).to_have_url("http://app.test" + href)


def test_chart_renders_real_points_and_empty_history(page: Page):
    screen = detail("memecoins", {"coin": fixtures.sample("memecoins"), "history": []})
    screen.pop("quote_url")
    screen["series"] = [
        {"time": "2026-09-12T12:00:00Z", "value": 1},
        {"time": "2026-09-12T13:00:00Z", "value": 2},
    ]
    open_screen(page, screen)
    expect(page.get_by_role("img", name="Price history")).to_be_visible()
    assert page.locator(".chart-line").get_attribute("d").startswith("M")
    screen["series"] = []
    page.unroute("http://app.test/")
    open_screen(page, screen)
    expect(page.get_by_text("Price history will appear here.")).to_be_visible()
    expect(page.locator(".price-chart")).to_be_hidden()


def test_call_requires_confirmation_and_keeps_internal_error_private(page: Page):
    screen = detail("memecoins", {"coin": fixtures.sample("memecoins"), "can_call": True})
    screen.pop("quote_url")
    submitted = []

    def record(route):
        submitted.append(route.request.post_data_json)
        route.fulfill(status=409, json={"detail": fixtures.SENTINEL})

    page.route("**/api/memecoins/*/calls", record)
    open_screen(page, screen, {"id": "test-user"})
    page.get_by_role("button", name="Make Call", exact=True).click()
    assert submitted == []
    page.get_by_role("button", name="Cancel", exact=True).click()
    assert submitted == []
    page.get_by_role("button", name="Make Call", exact=True).click()
    page.get_by_role("button", name="Confirm Call", exact=True).click()
    expect(page.get_by_role("status")).to_have_text("Please try again when the price is current.")
    assert submitted == [{}]
    expect(page.get_by_text(fixtures.SENTINEL)).to_have_count(0)
