"""Refresh keeps late detail views and list state controls in sync."""

from __future__ import annotations

import copy
import importlib.util
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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


def assets(page, held=None, script="market-anchor.js"):
    def serve(route):
        if held is not None and script in route.request.url:
            held.append(route)
            return
        asset = route.request.url.split("/static/", 1)[1].split("?")[0]
        route.fulfill(path=str(ROOT / "web/static" / asset))

    page.route("https://app.test/static/**", serve)


def sport(signal):
    event = screens.sample("sports")
    now = datetime.now(UTC)
    event.update(status="pre", start_time=(now + timedelta(days=1)).isoformat())
    if signal:
        event["prediction"] = {
            "selection": "home",
            "signal": "watch",
            "home_probability": 0.6,
            "away_probability": 0.4,
            "home_market_probability": 0.57 if signal == "above" else 0.63,
            "model_version": "team-form-v1",
            "observed_at": now.isoformat(),
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


def ratified_board(page, market):
    base = screens.sample(market)
    state = {
        "items": [
            {
                **base,
                "ticker": "FIRST",
                "symbol": "FIRST",
                "id": "first",
                "trade_state": "TRIGGERED",
                "ratification": {"ratified": True},
            },
            {
                **base,
                "ticker": "SECOND",
                "symbol": "SECOND",
                "id": "second",
                "trade_state": "ARMED",
                "ratification": {"ratified": True},
            },
            {
                **base,
                "ticker": "THIRD",
                "symbol": "THIRD",
                "id": "third",
                "trade_state": "TRIGGERED",
                "ratification": {"ratified": False},
            },
        ],
        "reads": [],
    }

    def serve(route):
        url = urlsplit(route.request.url)
        state["reads"].append(url)
        query = parse_qs(url.query).get("q", [""])[0]
        board = listing(market, state["items"], query=query)
        route.fulfill(content_type="text/html", body=screens.render(board, query_string=url.query))

    page.clock.install()
    assets(page)
    page.route(re.compile(r"https://app\.test/(?:memecoins)?(?:\?.*)?$"), serve)
    page.route("https://app.test/api/**", lambda route: route.fulfill(json={"charts": {}}))
    page.goto("https://app.test/" + ("memecoins" if market == "memecoins" else ""))
    return state


@pytest.mark.parametrize("market", ["stocks", "memecoins"])
@pytest.mark.parametrize("width", [390, 1280])
def test_ratified_toggle_combines_with_state_and_survives_reload(page, market, width, tmp_path):
    page.set_viewport_size({"width": width, "height": 844})
    ratified_board(page, market)
    control = page.locator("[data-ratified-filter]")
    expect(control).to_have_text("2 Ratified")
    control.click()
    expect(control).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".ticker:visible")).to_have_count(2)
    expect(page).to_have_url(re.compile(r"[?&]ratified=1"))
    expect(page.locator('button[data-tag="running"]')).to_have_text("1 running")
    page.locator('button[data-tag="running"]').click()
    expect(page.locator(".ticker:visible")).to_have_count(1)
    expect(control).to_have_text("1 Ratified")
    expect(page.locator(".ticker:visible .ticker-name strong")).to_have_text("FIRST")
    page.locator('button[data-tag="running"]').click()
    expect(page.locator(".ticker:visible")).to_have_count(2)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"ratified-{market}-{width}.png"), full_page=True)
    page.reload()
    expect(control).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".ticker:visible")).to_have_count(2)
    page.locator(".chip-all").click()
    expect(page.locator(".ticker:visible")).to_have_count(3)
    expect(control).to_have_attribute("aria-pressed", "false")
    assert "ratified" not in parse_qs(urlsplit(page.url).query)


@pytest.mark.parametrize("market", ["stocks", "memecoins"])
def test_ratified_toggle_tracks_live_changes_and_recovers_from_empty(page, market):
    state = ratified_board(page, market)
    control = page.locator("[data-ratified-filter]")
    control.click()
    for item in state["items"]:
        item["ratification"]["ratified"] = False
    with page.expect_response(page.url):
        page.clock.fast_forward(60_000)
    expect(control).to_have_text("0 Ratified")
    expect(control).to_be_focused()
    expect(control).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".ticker:visible")).to_have_count(0)
    expect(page.locator("[data-filter-empty]")).to_have_text(
        "Ratified results will appear here.Show all"
    )
    page.get_by_role("button", name="Show all", exact=True).click()
    expect(page.locator(".ticker:visible")).to_have_count(3)
    expect(control).to_have_attribute("aria-pressed", "false")
    assert "ratified" not in parse_qs(urlsplit(page.url).query)
    control.click()
    state["items"][2]["ratification"]["ratified"] = True
    with page.expect_response(page.url):
        page.clock.fast_forward(60_000)
    expect(control).to_have_text("1 Ratified")
    expect(page.locator(".ticker:visible .ticker-name strong")).to_have_text("THIRD")
    control.click()
    expect(page.locator(".ticker:visible")).to_have_count(3)


@pytest.mark.parametrize("market", ["stocks", "memecoins"])
def test_ratified_selection_applies_to_search_suggestions_and_submission(page, market):
    state = ratified_board(page, market)
    page.locator("[data-ratified-filter]").click()
    search = page.get_by_role("combobox")
    search.fill("THIRD")
    with page.expect_response(re.compile(r"[?&]q=THIRD")):
        page.clock.fast_forward(300)
    expect(page.get_by_role("option")).to_have_count(0)
    assert parse_qs(state["reads"][-1].query)["ratified"] == ["1"]
    search.fill("FIRST")
    with page.expect_response(re.compile(r"[?&]q=FIRST")):
        page.clock.fast_forward(300)
    expect(page.get_by_role("option")).to_have_count(1)
    search.press("Enter")
    expect(page).to_have_url(re.compile(r"[?&]q=FIRST"))
    assert parse_qs(urlsplit(page.url).query)["ratified"] == ["1"]
    expect(page.locator("[data-ratified-filter]")).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".ticker:visible .ticker-name strong")).to_have_text("FIRST")


def test_late_assessment_script_consumes_completed_detail_refresh(page):
    screen = detail("memecoins", {"coin": screens.sample("memecoins")})
    fresh = copy.deepcopy(screen)
    fresh["item"]["value"] = "$123.00"
    fresh["item"]["assessment"].update(
        label="Runner score", value=72, unit="pts", drivers=[{"label": "Saved liquidity"}]
    )
    held = []
    assets(page, held)
    page.route("https://app.test/api/**", lambda route: route.fulfill(json=fresh))
    page.route(
        "https://app.test/",
        lambda route: route.fulfill(content_type="text/html", body=screens.render(screen)),
    )
    page.goto("https://app.test/", wait_until="commit")
    expect(page.locator("[data-value]")).to_have_text("$123.00")
    evidence = page.locator("[data-market-assessment]")
    summary = evidence.locator("summary")
    # Nothing found yet, so the block waits out of sight.
    expect(evidence).to_be_hidden()
    expect(summary).to_have_text("Token evidence · no chain findings saved")
    assert (
        page.evaluate(
            "document.getElementById('screenData').ratiScreenDetail.item.assessment.value"
        )
        == 72
    )
    assert len(held) == 1
    held[0].fulfill(path=str(ROOT / "web/static/market-anchor.js"))
    page.wait_for_load_state("load")
    expect(summary).to_have_text("Token evidence · 1 finding")
    expect(evidence).to_be_visible()


def test_state_change_refreshes_counts_and_all_recovers_the_list(page):
    state = start_list(page, sport("above"))
    page.locator('.chip[data-tag="above"]').click()
    refresh(page, state, sport("below"))
    expect(page.locator('.chip[data-tag="above"]')).to_have_text("0 above")
    expect(page.locator('.chip[data-tag="above"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator('.chip[data-tag="below"]')).to_have_text("1 below")
    expect(page.locator(".ticker:visible")).to_have_count(0)
    expect(page.locator("[data-filter-empty]")).to_contain_text("No results in this state.")
    page.locator(".chip-all").click()
    expect(page.locator(".ticker:visible")).to_have_count(1)
    expect(page.locator('[data-tag-filters] [data-tag="above"]')).to_have_count(0)
    expect(page.locator("[data-filter-empty]")).to_be_hidden()
    page.locator('.chip[data-tag="below"]').click()
    expect(page.locator(".ticker:visible")).to_have_count(1)


def test_disappeared_filters_preserve_selection_then_recover(page):
    state = start_list(page, sport("above"))
    page.locator('.chip[data-tag="above"]').click()
    refresh(page, state, sport(None))
    expect(page.locator('.chip[data-tag="above"]')).to_have_text("0 above")
    expect(page.get_by_role("button", name="Show all", exact=True)).to_be_visible()
    refresh(page, state, sport("above"))
    expect(page.locator('.chip[data-tag="above"]')).to_have_text("1 above")
    expect(page.locator('.chip[data-tag="above"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator(".ticker:visible")).to_have_count(1)
    expect(page.locator("[data-filter-empty]")).to_have_count(0)
    refresh(page, state, listing("sports", []))
    expect(page.get_by_role("button", name="Show all", exact=True)).to_be_visible()
    page.get_by_role("button", name="Show all", exact=True).click()
    expect(page.locator("[data-tag-filters]")).to_have_count(0)
    expect(page.get_by_role("heading", name="A quiet moment")).to_be_visible()
    refresh(page, state, sport("below"))
    expect(page.locator('.chip[data-tag="below"]')).to_have_text("1 below")
    page.locator('.chip[data-tag="below"]').click()
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
    held[1].fulfill(content_type="text/html", body=screens.render(sport("below")))
    expect(page.locator('.chip[data-tag="below"]')).to_have_text("1 below")
    with page.expect_response("https://app.test/"):
        held[0].fulfill(content_type="text/html", body=screens.render(sport("above")))
    # A later round trip ensures the fetch continuation has handled the old response.
    page.evaluate("() => new Promise(resolve => setTimeout(resolve, 0))")
    expect(page.locator('.chip[data-tag="below"]')).to_have_text("1 below")
    expect(page.locator('.chip[data-tag="above"]')).to_have_count(0)
    expect(page.locator(".ticker")).to_have_attribute("data-tag", "below")
    page.locator('.chip[data-tag="below"]').click()
    expect(page.locator(".ticker:visible")).to_have_count(1)


def test_late_coin_map_uses_latest_score(page):
    raw = screens.sample("memecoins")
    data = {"coin": raw, "history": []}
    fresh = detail(
        "memecoins",
        {
            "coin": {
                **raw,
                "price": 123,
                "score": 72,
                "rug_score": 30,
                "score_detail": {
                    "drivers": [{"key": "market", "label": "Market", "value": 80}],
                    "penalties": [{"key": "risk", "label": "Risk", "value": -8}],
                },
            },
            "history": [],
        },
    )
    held = []
    assets(page, held, "memecoin-replay.js")
    page.route("https://app.test/api/**", lambda route: route.fulfill(json=fresh))
    html = screens._render_template(
        "simple_coin_detail.html", {"detail": data, "active_call": None, "calls": []}
    )
    page.route(
        "https://app.test/", lambda route: route.fulfill(content_type="text/html", body=html)
    )
    page.goto("https://app.test/", wait_until="commit")
    expect(page.locator("[data-value]")).to_have_text("$123.00")
    assert len(held) == 1
    held[0].fulfill(path=str(ROOT / "web/static/memecoin-replay.js"))
    page.wait_for_load_state("load")
    expect(page.locator(".map-center-score")).to_have_text("72")
    expect(page.locator(".map-glyph")).to_have_attribute("data-band", "3")
    expect(page.locator(".map-glyph")).to_have_attribute("data-risk", "detected")
    expect(page.locator(".map-score-segment")).to_have_count(1)
    page.locator(".map-score-segment").first.press("Enter")
    expect(page.locator("[data-replay-selection]")).to_contain_text("80 pts")
    page.locator(".map-score-center").press("Enter")
    expect(page.locator(".map-center-score")).to_have_text("72")
    expect(page.locator("[data-replay-selection] h3")).to_have_count(0)
