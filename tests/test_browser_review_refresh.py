"""Refresh keeps late detail views and list state controls in sync."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
from playwright.sync_api import expect

from runner_web.market_screens import detail, listing

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser
spec = importlib.util.spec_from_file_location(
    "review_screens", ROOT / "tests/test_market_screens.py"
)
screens = importlib.util.module_from_spec(spec)
spec.loader.exec_module(screens)


def assets(page, held=None):
    def serve(route):
        if held is not None and "market-anchor.js" in route.request.url:
            held.append(route)
            return
        asset = route.request.url.split("/static/", 1)[1].split("?")[0]
        route.fulfill(path=str(ROOT / "web/static" / asset))

    page.route("https://app.test/static/**", serve)


def sport(signal):
    event = screens.sample("sports")
    if signal:
        event["prediction"] = {
            "selection": "home",
            "signal": signal,
            "home_probability": 0.6,
            "home_market_probability": 0.57,
            "edge": 0.03,
            "observed_at": "2026-09-19T12:00:00Z",
        }
    return listing("sports", [event])


def start_list(page, initial):
    page.clock.install()
    assets(page)
    state = {"html": screens.render(initial)}
    page.route(
        "https://app.test/",
        lambda route: route.fulfill(content_type="text/html", body=state["html"]),
    )
    page.goto("https://app.test/")
    return state


def refresh(page, state, screen):
    state["html"] = screens.render(screen)
    with page.expect_response("https://app.test/"):
        page.clock.fast_forward(60_000)


def test_late_assessment_script_consumes_completed_detail_refresh(page):
    screen = detail("memecoins", {"coin": screens.sample("memecoins"), "history": []})
    fresh = copy.deepcopy(screen)
    fresh["item"]["value"] = "$123.00"
    fresh["item"]["assessment"].update(label="Runner score", value=72, unit="pts")
    held = []
    assets(page, held)
    page.route("https://app.test/api/**", lambda route: route.fulfill(json=fresh))
    page.route(
        "https://app.test/",
        lambda route: route.fulfill(content_type="text/html", body=screens.render(screen)),
    )
    page.goto("https://app.test/", wait_until="commit")
    expect(page.locator("[data-value]")).to_have_text("$123.00")
    expect(page.locator("[data-assessment-value]")).to_have_text("—")
    assert (
        page.evaluate(
            "document.getElementById('screenData').ratiScreenDetail.item.assessment.value"
        )
        == 72
    )
    assert len(held) == 1
    held[0].fulfill(path=str(ROOT / "web/static/market-anchor.js"))
    page.wait_for_load_state("load")
    expect(page.locator("[data-assessment-value]")).to_have_text("72pts")
    expect(page.locator("[data-assessment-label]")).to_have_text("Runner score")


def test_state_change_refreshes_counts_and_all_recovers_the_list(page):
    state = start_list(page, sport("lean"))
    page.locator('.chip[data-tag="lean"]').click()
    refresh(page, state, sport("pass"))
    expect(page.locator('.chip[data-tag="lean"]')).to_have_text("0 lean")
    expect(page.locator('.chip[data-tag="lean"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator('.chip[data-tag="pass"]')).to_have_text("1 pass")
    expect(page.locator(".ticker:visible")).to_have_count(0)
    expect(page.locator("[data-filter-empty]")).to_contain_text("No results in this state.")
    page.locator(".chip-all").click()
    expect(page.locator(".ticker:visible")).to_have_count(1)
    expect(page.locator('[data-tag-filters] [data-tag="lean"]')).to_have_count(0)
    expect(page.locator("[data-filter-empty]")).to_be_hidden()
    page.locator('.chip[data-tag="pass"]').click()
    expect(page.locator(".ticker:visible")).to_have_count(1)


def test_disappeared_filters_preserve_selection_then_recover(page):
    state = start_list(page, sport("lean"))
    page.locator('.chip[data-tag="lean"]').click()
    refresh(page, state, sport(None))
    expect(page.locator('.chip[data-tag="lean"]')).to_have_text("0 lean")
    expect(page.get_by_role("button", name="Show all", exact=True)).to_be_visible()
    refresh(page, state, sport("lean"))
    expect(page.locator('.chip[data-tag="lean"]')).to_have_text("1 lean")
    expect(page.locator('.chip[data-tag="lean"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".ticker:visible")).to_have_count(1)
    expect(page.locator("[data-filter-empty]")).to_have_count(0)
    refresh(page, state, listing("sports", []))
    expect(page.get_by_role("button", name="Show all", exact=True)).to_be_visible()
    page.get_by_role("button", name="Show all", exact=True).click()
    expect(page.locator("[data-tag-filters]")).to_have_count(0)
    expect(page.get_by_role("heading", name="A quiet moment")).to_be_visible()
    refresh(page, state, sport("pass"))
    expect(page.locator('.chip[data-tag="pass"]')).to_have_text("1 pass")
    page.locator('.chip[data-tag="pass"]').click()
    expect(page.locator(".ticker:visible")).to_have_count(1)


def test_older_list_response_keeps_latest_rows_and_filters(page):
    start_list(page, sport(None))
    held = []

    def hold(route):
        held.append(route)
        page.evaluate("count => window.heldRefreshCount = count", len(held))

    page.route("https://app.test/", hold)
    with page.expect_request("https://app.test/"):
        page.clock.fast_forward(60_000)
    with page.expect_request("https://app.test/"):
        page.clock.fast_forward(60_000)
    page.wait_for_function("window.heldRefreshCount === 2")
    assert len(held) == 2
    held[1].fulfill(content_type="text/html", body=screens.render(sport("pass")))
    expect(page.locator('.chip[data-tag="pass"]')).to_have_text("1 pass")
    with page.expect_response("https://app.test/"):
        held[0].fulfill(content_type="text/html", body=screens.render(sport("lean")))
    # A later round trip ensures the fetch continuation has handled the old response.
    page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")
    expect(page.locator('.chip[data-tag="pass"]')).to_have_text("1 pass")
    expect(page.locator('.chip[data-tag="lean"]')).to_have_count(0)
    expect(page.locator(".ticker")).to_have_attribute("data-tag", "pass")
    page.locator('.chip[data-tag="pass"]').click()
    expect(page.locator(".ticker:visible")).to_have_count(1)
