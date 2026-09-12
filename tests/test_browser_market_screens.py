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
    screen.pop("refresh_url")
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
    screen.pop("refresh_url")
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
    expect(page.locator("[data-action-status]")).to_have_text(
        "Please try again when the price is current."
    )
    assert submitted == [{"expected_price": fixtures.sample("memecoins")["price"]}]
    expect(page.get_by_text(fixtures.SENTINEL)).to_have_count(0)


screen_client = fixtures.screen_client


@pytest.mark.parametrize("market", ["memecoins", "sports"])
@pytest.mark.parametrize("view", ["list", "map", "detail"])
@pytest.mark.parametrize("width", [390, 1280])
def test_actual_routes_refresh_pending_values(
    page: Page, screen_client, monkeypatch, market, view, width
):
    from urllib.parse import urlsplit

    from runner_web import main as web

    coin = {**fixtures.sample("memecoins"), "stale": True}
    event = fixtures.sample("sports")
    event.update(status="pre", away_score=0, home_score=0)
    event["view_state"].update(score_available=False, label="Game started")
    monkeypatch.setattr(web, "memecoin_market", lambda **kw: {"rows": [coin]})
    monkeypatch.setattr(web, "market_actor_map", lambda *a: {})
    monkeypatch.setattr(
        web,
        "_memecoin_detail_payload",
        lambda *a: {"coin": coin, "calls": [], "can_call": not coin["stale"]},
    )
    monkeypatch.setattr(web, "product_for_request", lambda *a: market)
    monkeypatch.setattr(web, "sports_slate", lambda *a: {"events": [event]})
    monkeypatch.setattr(web, "sports_event", lambda *a: event)

    def serve(route):
        url = urlsplit(route.request.url)
        response = screen_client.get(url.path + ("?" + url.query if url.query else ""))
        route.fulfill(
            status=response.status_code,
            headers=dict(response.headers),
            body=response.content,
        )

    page.route("http://app.test/**", serve)
    page.set_viewport_size({"width": width, "height": 844})
    page.clock.install()
    path = (
        "/memecoins/coin/solana-test"
        if market == "memecoins" and view == "detail"
        else "/game/nba:123"
        if view == "detail"
        else f"/memecoins?view={view}"
        if market == "memecoins"
        else f"/?league=nba&view={view}"
    )
    page.goto("http://app.test" + path)
    surface = page.locator("[data-live-surface]")
    pending = "Price paused" if market == "memecoins" else "Score pending"
    expect(surface.get_by_text(pending, exact=True).first).to_be_visible()
    for price, away, home in [(0.000020, 72, 68), (0.000021, 74, 70)]:
        coin.update(stale=False, price=price)
        event.update(status="in", away_score=away, home_score=home)
        event["view_state"].update(score_available=True, label="Live")
        page.clock.fast_forward(60000)
        expected = f"${price:.6f}".rstrip("0") if market == "memecoins" else f"{away} – {home}"
        expect(
            page.locator("[data-live-surface]").get_by_text(expected, exact=True)
        ).to_be_visible()
        expect(page.locator("[data-live-surface]").get_by_text(pending, exact=True)).to_have_count(
            0
        )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.get_by_text(fixtures.SENTINEL)).to_have_count(0)


changing_detail = fixtures.changing_detail


@pytest.fixture
def live_detail_page(page, screen_client, changing_detail):
    from urllib.parse import urlsplit

    def serve(route):
        url = urlsplit(route.request.url)
        response = screen_client.get(url.path + ("?" + url.query if url.query else ""))
        route.fulfill(
            status=response.status_code, headers=dict(response.headers), body=response.content
        )

    page.route("http://app.test/**", serve)
    page.clock.install()
    return page


@pytest.mark.parametrize(
    "market,subject,path",
    [
        ("stocks", "OPK", "/t/OPK"),
        ("memecoins", "solana-test", "/memecoins/coin/solana-test"),
    ],
)
@pytest.mark.parametrize("width", [390, 1280])
def test_actual_detail_refresh_keeps_call_confirmation_and_focus(
    live_detail_page, changing_detail, market, subject, path, width
):
    page = live_detail_page
    page.set_viewport_size({"width": width, "height": 844})
    submissions = []

    def commit(route):
        submissions.append(route.request.post_data_json)
        changing_detail["active"] = False
        route.fulfill(json={"call": {"status": "closed"}})

    page.route("**/api/*calls/**/close", commit)
    page.goto("http://app.test" + path)
    expect(page.locator("[data-call-outcome]")).to_contain_text(["+4.0%"])
    for price, value in [(3, "+100.0%"), (4.5, "+200.0%")]:
        changing_detail["price"] = price
        page.clock.fast_forward(60000)
        expect(page.locator("[data-value]")).to_have_text(f"${price:.2f}")
        expect(page.locator("[data-call-outcome]")).to_contain_text([value])
    page.get_by_role("button", name="Close Call", exact=True).click()
    confirm = page.get_by_role("button", name="Confirm Call", exact=True)
    expect(confirm).to_be_focused()
    changing_detail["price"] = 6
    page.clock.fast_forward(60000)
    expect(page.locator("[data-value]")).to_have_text("$6.00")
    expect(page.locator(".call-confirm")).to_be_visible()
    expect(confirm).to_be_focused()
    expect(page.locator("[data-confirm-terms]")).to_contain_text("$4.5")
    confirm.click()
    expect(page.locator("[data-confirm-status]")).to_contain_text("confirm again")
    assert submissions == []
    expect(page.locator("[data-confirm-terms]")).to_contain_text("$6")
    confirm.click()
    expect(page.get_by_role("button", name="Make Call", exact=True)).to_be_visible()
    assert submissions == [{"expected_price": 6}]
    expect(page.get_by_role("button", name="Close Call", exact=True)).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_coin_paused_action_recovers_and_settlement_preserves_dialog(
    live_detail_page, changing_detail
):
    page = live_detail_page
    changing_detail["paused"] = True
    page.goto("http://app.test/memecoins/coin/solana-test")
    expect(page.get_by_text("Price paused", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Close Call", exact=True)).to_have_count(0)
    changing_detail["paused"] = False
    page.clock.fast_forward(60000)
    page.get_by_role("button", name="Close Call", exact=True).click()
    expect(page.locator(".call-confirm")).to_be_visible()
    changing_detail["active"] = False
    page.clock.fast_forward(60000)
    expect(page.get_by_role("button", name="Make Call", exact=True)).to_be_visible()
    expect(page.locator(".call-confirm")).to_be_visible()
    page.get_by_role("button", name="Confirm Call", exact=True).click()
    expect(page.locator("[data-confirm-status]")).to_contain_text("This Call has changed")


@pytest.mark.parametrize(
    "market,subject,path",
    [
        ("stocks", "OPK", "/t/OPK"),
        ("memecoins", "solana-test", "/memecoins/coin/solana-test"),
    ],
)
def test_late_detail_response_is_discarded_after_navigation(
    live_detail_page, screen_client, changing_detail, market, subject, path
):
    page = live_detail_page
    page.goto("http://app.test" + path)
    expect(page.locator("[data-call-outcome]")).to_contain_text(["+4.0%"])
    held = []
    endpoint = f"/api/screens/{market}/{subject}/detail"
    page.route("http://app.test" + endpoint, lambda route: held.append(route))
    changing_detail["price"] = 9
    with page.expect_request("http://app.test" + endpoint):
        page.clock.fast_forward(60000)
    page.evaluate("history.pushState({}, '', '/t/OTHER')")
    with page.expect_response("http://app.test" + endpoint):
        held[0].fulfill(json=screen_client.get(endpoint).json())
    # Yield through a frame after the fetch continuation.
    page.clock.run_for(20)
    expect(page.locator("[data-value]")).to_have_text("$1.56")
    expect(page.locator("[data-call-outcome]")).to_contain_text(["+4.0%"])


@pytest.mark.parametrize("market", ["stocks", "memecoins", "sports"])
@pytest.mark.parametrize("width", [390, 1280])
def test_call_record_keeps_choice_terms_and_reward_readable(page, market, width):
    raw = fixtures.sample(market)
    saved = {
        "status": "closed",
        "entry_price": 1.5,
        "exit_price": 3,
        "entry_at": "2026-09-11T18:00:00Z",
        "flash_reward": 100,
    }
    if market == "sports":
        saved = {
            "status": "settled",
            "selection": "away",
            "american_odds": 130,
            "created_at": "2026-09-11T18:00:00Z",
            "result": "loss",
            "reward_flash": 0,
        }
        screen = detail(market, raw, my_pick=saved)
    else:
        data = {"coin": raw} if market == "memecoins" else {"ticker": "OPK", "current": raw}
        screen = detail(market, data, active_call=saved)
    page.set_viewport_size({"width": width, "height": 844})
    open_screen(page, screen)
    record = page.get_by_role("region", name="Your Call")
    expect(record).to_be_visible()
    expect(record.get_by_text(screen["call"]["choice"], exact=True)).to_be_visible()
    expect(record.get_by_text(screen["call"]["entry"], exact=True)).to_be_visible()
    expect(record.get_by_text(screen["call"]["terms"], exact=True)).to_be_visible()
    expect(record.get_by_text(screen["call"]["reward"], exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
