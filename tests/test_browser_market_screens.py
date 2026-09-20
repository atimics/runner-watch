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
    expect(page.locator(".ticker-list")).to_be_visible()
    expect(page.locator(".ticker")).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".ticker").bounding_box()["y"] < 450
    expect(page.get_by_text(fixtures.SENTINEL)).to_have_count(0)


def test_tag_filter_chips_hide_and_show_rows(page: Page):
    items = [fixtures.scored_stock(), {**fixtures.sample("stocks"), "ticker": "SLOW"}]
    open_screen(page, listing("stocks", items))
    expect(page.locator(".ticker")).to_have_count(2)
    page.get_by_role("button", name=re.compile("running")).click()
    expect(page.locator(".ticker:visible")).to_have_count(1)
    page.get_by_role("button", name=re.compile("running")).click()
    expect(page.locator(".ticker:visible")).to_have_count(2)


def test_narrow_stock_rows_are_single_line(page: Page):
    page.set_viewport_size({"width": 390, "height": 844})
    open_screen(page, listing("stocks", [fixtures.scored_stock()]))
    row = page.locator(".ticker").first
    expect(row).to_be_visible()
    box = row.bounding_box()
    assert box is not None and box["height"] <= 62
    selectors = (".tag", ".ticker-name", ".ticker-value", ".ticker-score")
    children = [row.locator(selector).bounding_box() for selector in selectors]
    children = [item for item in children if item is not None]
    assert max(item["y"] for item in children) - min(item["y"] for item in children) <= 4


def test_chart_renders_real_points_and_empty_history(page: Page):
    screen = detail("memecoins", {"coin": fixtures.sample("memecoins"), "history": []})
    screen.pop("refresh_url")
    screen["series"] = [
        {"time": "2026-09-12T12:00:00Z", "value": 1},
        {"time": "2026-09-12T13:00:00Z", "value": 2},
    ]
    open_screen(page, screen)
    expect(page.get_by_role("img", name=re.compile("Price history:"))).to_be_visible()
    assert page.locator(".chart-line").get_attribute("d").startswith("M")
    screen["series"] = []
    page.unroute("http://app.test/")
    open_screen(page, screen)
    expect(page.get_by_text("Price history will appear here.")).to_be_visible()
    expect(page.locator(".price-chart")).to_be_hidden()


def test_call_requires_confirmation_and_keeps_internal_error_private(page: Page):
    screen = detail("memecoins", {"coin": fixtures.sample("memecoins"), "can_call": True})
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
    # The Call record reads as one line: every fact shares a baseline, with the
    # share link trailing them rather than sitting under the list.
    labels = page.locator(".call-record .facts dt")
    expect(labels).to_have_count(2)
    tops = labels.evaluate_all(
        "nodes => nodes.map(node => Math.round(node.getBoundingClientRect().top))"
    )
    assert len(set(tops)) == 1, tops
    record = page.locator(".call-record").bounding_box()
    # Entry and status share a row; the actions may wrap under them on mobile.
    assert record["height"] < 120, record
    share = page.locator(".call-record .call-share")
    if share.count():
        expect(share).to_be_visible()
        assert share.bounding_box()["y"] < record["y"] + record["height"]
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
def test_call_record_shows_only_entry_and_status(page, market, width):
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
    # Entry and status only: the choice, settlement terms and reward were noise.
    expect(record.locator("dt")).to_have_text(["Entry", "Status"])
    expect(record.locator("[data-call-entry]")).to_contain_text(screen["call"]["entry"])
    expect(record.locator("[data-call-outcome]")).to_contain_text(screen["call"]["outcome"])
    expect(record.locator("[data-call-choice]")).to_have_count(0)
    expect(record.locator("[data-call-terms]")).to_have_count(0)
    expect(record.locator("[data-call-reward]")).to_have_count(0)
    # A settled Call has nothing left to close.
    expect(record.get_by_role("button", name="Close Call")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [390, 1280])
def test_an_open_call_offers_the_way_out_and_does_not_overflow(page, width):
    screen = detail(
        "stocks",
        {"ticker": "OPK", "current": fixtures.sample("stocks")},
        active_call={
            "status": "active",
            "entry_price": 2.28,
            "entry_at": "2026-09-11T18:00:00Z",
            "public_id": "call-1",
        },
    )
    page.set_viewport_size({"width": width, "height": 844})
    open_screen(page, screen, user={"id": "reader", "username": "reader", "display_name": "Reader"})

    record = page.get_by_role("region", name="Your Call")
    expect(record).to_be_visible()
    expect(record.get_by_role("button", name="Close Call")).to_be_visible()
    # The close control lives with the Call, not duplicated below it.
    expect(page.locator("[data-screen-actions]").locator("[data-action]")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("width", [320, 1280])
@pytest.mark.parametrize("end,expected", [(100.01, "+0.01%"), (200, "+100%"), (100, "+0%")])
def test_chart_scopes_magnitude_and_period_separately_from_daily_quote(page, width, end, expected):
    screen = detail("stocks", {"ticker": "OPK", "current": fixtures.sample("stocks")})
    screen.pop("refresh_url")
    screen.pop("chart_url")
    screen["series"] = [
        {"time": "2026-09-10T12:00:00Z", "value": 100},
        {"time": "2026-09-12T12:00:00Z", "value": end},
    ]
    page.set_viewport_size({"width": width, "height": 844})
    open_screen(page, screen)
    label = page.locator(".price-chart").get_attribute("aria-label")
    assert "$100.00 →" in label
    assert expected in label
    assert "Sep 10" in label and "Sep 12" in label
    expect(page.locator("[data-chart-summary]")).to_have_count(0)
    expect(page.locator("[data-chart-start]")).to_have_count(0)
    expect(page.locator("[data-chart-end]")).to_have_count(0)
    expect(page.locator("[data-change]")).to_have_text("+5.4%")
    expect(page.locator(".quote-scope")).to_contain_text("Daily change")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_the_gap_is_drawn_as_an_honest_dashed_stretch(page):
    """Past the last saved bar the chart keeps going, but as a dashed projection
    with a band and a label, never as invented candles."""

    screen = detail("stocks", {"ticker": "OPK", "current": fixtures.sample("stocks")})
    screen.pop("refresh_url")
    screen.pop("chart_url")
    screen["series"] = [
        {"time": "2026-09-12T12:00:00Z", "value": 100},
        {"time": "2026-09-12T12:05:00Z", "value": 101},
    ]
    screen["gap"] = {
        "anchor_time": "2026-09-12T12:05:00Z",
        "anchor_price": 101,
        "state": "stale",
        "market_open": False,
        "latency_seconds": 3600,
        "gap_minutes": 60,
        "band_pct": 0.4,
        "label": "Market closed · dashed line is our flat projection (±0.4%)",
        "path": [
            {"time": "2026-09-12T12:35:00Z", "price": 101, "low": 100.8, "high": 101.2},
            {"time": "2026-09-12T13:05:00Z", "price": 101, "low": 100.6, "high": 101.4},
        ],
    }
    open_screen(page, screen)
    line = page.locator(".chart-gap-line")
    # A flat projection is a horizontal dashed line, so it has no height of its
    # own to be "visible" by; assert its geometry and dash pattern instead.
    expect(line).to_have_count(1)
    dash = line.evaluate("node => getComputedStyle(node).strokeDasharray")
    assert dash.replace("px", "").startswith("5"), dash
    expect(page.locator(".chart-gap-band")).to_be_visible()
    expect(page.locator(".chart-gap-note")).to_contain_text("Market closed")
    # The dashed stretch runs from the last bar to the clock, so the axis now
    # reaches the right edge instead of stopping at the saved data.
    offsets = [
        float(value)
        for value in re.findall(r"[ML]([-\d.]+),", line.get_attribute("d"))
    ]
    assert offsets[0] != offsets[-1]
    assert offsets[-1] == pytest.approx(792, abs=1)
    label = page.locator(".price-chart").get_attribute("aria-label")
    assert "projection" in label


def test_single_chart_point_and_empty_refresh_clear_old_period(page):
    screen = detail("memecoins", {"coin": fixtures.sample("memecoins")})
    screen.pop("refresh_url")
    screen["series"] = [{"time": "2026-09-12T12:00:00Z", "value": 0.000018}]
    open_screen(page, screen)
    expect(page.locator(".chart-point")).to_be_visible()
    expect(page.locator(".price-chart")).to_have_attribute(
        "aria-label", re.compile("One saved price: \\$0\\.000018.*Sep 12")
    )
    screen["series"] = []
    page.unroute("http://app.test/")
    open_screen(page, screen)
    expect(page.locator(".price-chart")).to_be_hidden()
    expect(page.locator("[data-chart-status]")).to_have_text("Price history will appear here.")


def test_the_chart_is_drawn_in_the_colours_of_the_action_tag(page: Page):
    """A reader should see where a name turned from watch to setup to running
    without reading a table, so the line is split into one run per tag."""

    screen = detail("stocks", {"current": fixtures.scored_stock(), "ticker": "AAA"})
    screen.pop("refresh_url", None)
    screen.pop("chart_url", None)
    screen["series"] = [
        {"time": "2026-09-14T12:00:00Z", "value": 1},
        {"time": "2026-09-14T13:00:00Z", "value": 2},
        {"time": "2026-09-15T12:00:00Z", "value": 3},
        {"time": "2026-09-15T13:00:00Z", "value": 4},
        {"time": "2026-09-16T12:00:00Z", "value": 5},
        {"time": "2026-09-16T13:00:00Z", "value": 6},
    ]
    screen["states"] = [
        {"time": "2026-09-14T00:00:00Z", "tone": "watch"},
        {"time": "2026-09-15T06:00:00Z", "tone": "setup"},
        {"time": "2026-09-16T06:00:00Z", "tone": "running"},
    ]
    open_screen(page, screen)

    expect(page.locator(".chart-state.state-watch")).to_have_count(1)
    expect(page.locator(".chart-state.state-setup")).to_have_count(1)
    expect(page.locator(".chart-state.state-running")).to_have_count(1)
    legend = page.locator("[data-chart-state-legend] button")
    expect(legend).to_have_count(3)
    expect(legend).to_have_text(["Running", "Setup", "Watch"])
    expect(page.locator(".chart-state-label")).to_have_count(0)
    # Runs join up rather than leaving a gap at each change.
    watch_end = page.locator(".chart-state.state-watch").get_attribute("d").split("L")[-1]
    setup_start = page.locator(".chart-state.state-setup").get_attribute("d")
    assert setup_start.startswith("M" + watch_end)


def test_a_chart_without_state_history_keeps_one_plain_line(page: Page):
    screen = detail("stocks", {"current": fixtures.scored_stock(), "ticker": "AAA"})
    screen.pop("refresh_url", None)
    screen.pop("chart_url", None)
    screen["series"] = [
        {"time": "2026-09-16T12:00:00Z", "value": 1},
        {"time": "2026-09-16T13:00:00Z", "value": 2},
    ]
    screen["states"] = []
    open_screen(page, screen)

    expect(page.locator(".chart-state")).to_have_count(0)
    expect(page.locator(".chart-state-label")).to_have_count(0)
    expect(page.locator("[data-chart-state-legend]")).to_be_hidden()
    assert page.locator(".chart-line").get_attribute("d").startswith("M")


def _row(ticker: str, score: float) -> dict:
    row = {**fixtures.scored_stock(), "ticker": ticker, "score": score}
    row["score_detail"] = {
        "score": score,
        "penalties": [],
        "drivers": [
            {"key": "market", "label": "Market scanner", "value": score * 0.6},
            {"key": "news", "label": "News", "value": score * 0.4},
        ],
    }
    return row


def test_the_score_pie_reads_as_three_styles(page: Page):
    """Large and solid, large and ringed, small and ringed - told apart by size
    and fill before any number is read."""

    open_screen(page, listing("stocks", [_row("AAA", 88), _row("BBB", 52), _row("CCC", 24)]))

    bands = page.locator(".ticker-score")
    expect(bands).to_have_count(3)
    assert [bands.nth(i).get_attribute("data-band") for i in range(3)] == ["3", "2", "1"]
    sizes = [bands.nth(i).locator(".score-pie").bounding_box()["width"] for i in range(3)]
    assert sizes[0] == sizes[1] > sizes[2], sizes
    # The top band fills solid; the others have their middle taken out.
    masks = [
        bands.nth(i)
        .locator(".score-pie")
        .evaluate("el => getComputedStyle(el).webkitMaskImage || getComputedStyle(el).maskImage")
        for i in range(3)
    ]
    assert "gradient" not in (masks[0] or "none")
    assert all("gradient" in (mask or "") for mask in masks[1:])


def test_the_announced_row_wears_the_new_halo(page: Page):
    """The halo marks what the channel was most recently told, not what this
    reader happens not to have seen - one fact, the same for everyone, and it
    survives a reload."""

    rows = [_row("AAA", 88), _row("BBB", 52)]
    rows[0]["announced"] = True
    open_screen(page, listing("stocks", rows))

    expect(page.locator(".ticker.is-new")).to_have_count(1)
    expect(page.locator(".ticker.is-new")).to_have_attribute("href", "/t/AAA")


@pytest.mark.parametrize("market", ["stocks", "memecoins", "sports"])
def test_search_remembers_viewed_items_and_clears_history(page, market):
    source = fixtures.sample(market)
    payload = {"coin": source, "history": []} if market == "memecoins" else source
    screen = detail(market, payload)
    open_screen(page, screen)
    search = page.get_by_role("combobox")
    search.click()
    expect(page.get_by_text("Recently viewed", exact=True)).to_be_visible()
    option = page.get_by_role("option")
    expect(option).to_have_count(1)
    expect(option).to_contain_text(screen["item"]["name"])
    expect(option).to_have_attribute("href", screen["item"]["href"])
    page.reload()
    page.get_by_role("combobox").click()
    expect(page.get_by_role("option")).to_have_count(1)
    page.get_by_role("button", name="Clear recently viewed").click()
    expect(page.get_by_role("option")).to_have_count(0)
    expect(page.get_by_text("Items you view will appear here.")).to_be_visible()
    page.get_by_role("combobox").press("Escape")
    expect(page.locator(".search-popup")).to_be_hidden()


@pytest.mark.parametrize("width", [390, 1280])
def test_search_typeahead_keyboard_and_mobile_layout(page, width, tmp_path):
    page.set_viewport_size({"width": width, "height": 844})
    open_screen(page, listing("stocks", []))
    matches = listing("stocks", [fixtures.sample("stocks")])
    page.route("http://app.test/?*", lambda route: route.fulfill(
        content_type="text/html", body=fixtures.render(matches)
    ))
    search = page.get_by_role("combobox")
    search.fill("TE")
    expect(page.get_by_role("option")).to_have_count(1)
    expect(page.get_by_role("option")).to_contain_text(matches["rows"][0]["name"])
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"search-{width}.png"))
    search.press("ArrowDown")
    expect(page.get_by_role("option")).to_have_attribute("aria-selected", "true")
    href = matches["rows"][0]["href"]
    page.route(f"http://app.test{href}", lambda route: route.fulfill(body="Selected stock"))
    search.press("Enter")
    expect(page).to_have_url(f"http://app.test{href}")


def test_search_handles_storage_and_network_failure(page):
    page.add_init_script(
        "Storage.prototype.getItem = () => {throw new Error('blocked')};"
        "Storage.prototype.setItem = () => {throw new Error('blocked')};"
    )
    open_screen(page, listing("stocks", []))
    page.route("http://app.test/?*", lambda route: route.fulfill(status=503))
    search = page.get_by_role("combobox")
    search.fill("TEST")
    expect(page.get_by_text("Press Enter to search.")).to_be_visible()
    search.fill("")
    expect(page.get_by_text("Recently viewed", exact=True)).to_be_visible()
    page.locator(".brand").focus()
    expect(page.locator(".search-popup")).to_be_hidden()


def test_search_ignores_late_matches_and_keeps_history_in_view_order(page):
    page.add_init_script("""localStorage.setItem('rati:recently-viewed:stocks:v1', JSON.stringify([
      {name:'OLD', subtitle:'Older view', href:'/t/OLD'},
      {name:'OPK', subtitle:'Previous name', href:'/t/OPK'},
      {name:'Unsafe', href:'https://other.test/t/BAD'}
    ]));""")
    open_screen(page, detail("stocks", {"ticker": "OPK", "current": fixtures.sample("stocks")}))
    search = page.get_by_role("combobox")
    search.click()
    expect(page.get_by_role("option")).to_have_count(2)
    expect(page.get_by_role("option").first).to_contain_text("OPK")
    expect(page.get_by_role("option").last).to_contain_text("OLD")
    page.evaluate("""() => {
      const original = window.fetch; window.searchResponses = {};
      window.fetch = (url, options) => new URL(url, location.href).searchParams.has('q')
        ? new Promise(resolve => {
          window.searchResponses[new URL(url).searchParams.get('q')] = resolve;
        })
        : original(url, options);
    }""")
    search.fill("OLD")
    page.wait_for_function("Boolean(window.searchResponses.OLD)")
    search.fill("NEW")
    page.wait_for_function("Boolean(window.searchResponses.NEW)")
    for ticker in ["NEW", "OLD"]:
        html = fixtures.render(listing("stocks", [{**fixtures.sample("stocks"), "ticker": ticker}]))
        page.evaluate(
            "([ticker, html]) => window.searchResponses[ticker](new Response(html))",
            [ticker, html],
        )
    expect(page.get_by_role("option")).to_have_count(1)
    expect(page.get_by_role("option")).to_contain_text("NEW")
    search.press("Escape")
    expect(page.locator(".search-popup")).to_be_hidden()
