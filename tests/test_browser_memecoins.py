from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect
from starlette.requests import Request

from runner_web import main as web_main

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser
NOW = datetime.now(UTC).replace(microsecond=0)


def _coin(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "tiny-doge",
        "name": "Tiny Doge",
        "symbol": "DOGE",
        "price": 0.00000123,
        "price_label": "$0.00000123",
        "change_24h": 0,
        "volume_label": "$0",
        "market_cap_label": "unknown",
        "observed_at": NOW.isoformat(),
        "stale": False,
        "source_url": "https://www.coingecko.com/en/coins/tiny-doge",
        "detail_url": "/memecoins/coin/tiny-doge",
        "max_supply": 0,
        **overrides,
    }


def _market(**overrides: Any) -> dict[str, Any]:
    return {
        "rows": [_coin()],
        "total": 100,
        "query": "doge",
        "sort": "gainers",
        "status": "ok",
        "collected_at": NOW.isoformat(),
        "refresh_failed": False,
        **overrides,
    }


def _detail(**overrides: Any) -> dict[str, Any]:
    return {
        "coin": _coin(),
        "status": "ok",
        "collected_at": NOW.isoformat(),
        "refresh_failed": False,
        "source": "CoinGecko",
        "currency": "USD",
        "in_current_snapshot": True,
        "can_call": True,
        "evidence": {},
        "history": [
            {"observed_at": (NOW - timedelta(minutes=minute)).isoformat(), "price": value}
            for minute, value in [(15, 0.000001), (10, 0.0000011), (0, 0.00000123)]
        ],
        **overrides,
    }


def _call(**overrides: Any) -> dict[str, Any]:
    return {
        "public_id": "call-one",
        "coin_id": "tiny-doge",
        "symbol": "DOGE",
        "name": "Tiny Doge",
        "caller_handle": "QuietSignal",
        "status": "active",
        "entry_price_label": "$0.000001",
        "mark_price_label": "$0.00000123",
        "exit_price_label": None,
        "entry_at": NOW.isoformat(),
        "exit_at": None,
        "mark_at": NOW.isoformat(),
        "return_pct": 23,
        "detail_url": "/memecoins/coin/tiny-doge",
        **overrides,
    }


def _html(kind: str, *, signed_in: bool = False, **overrides: Any) -> str:
    path = "/memecoins/coin/tiny-doge" if kind == "detail" else "/memecoins/radar"
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"host", b"app.test")],
            "scheme": "http",
            "server": ("app.test", 80),
            "client": ("127.0.0.1", 1234),
            "query_string": b"q=doge&sort=gainers&view=radar",
        }
    )
    request.state.csp_nonce = "browser-test"
    context: dict[str, Any] = {
        "market": _market(),
        "detail": _detail(),
        "calls": [],
        "active_call": None,
        "list_path": "/memecoins/radar",
        "list_title": "Radar",
        "list_view": "radar",
        "back_url": "/memecoins/radar?q=doge&sort=gainers",
        "active_tab": "radar",
        "nav_product": "memecoins",
        "release_announcement_id": "memecoin-browser-checks",
        **overrides,
    }
    if signed_in:
        context.update(
            user={"id": "browser-user"},
            comment_avatar=web_main.comment_avatar_profile("Quiet Signal", "seed", "filing_sleuth"),
            flash_wallet={"balance": 0, "report_cost": 100},
            caller_summary={"average_return_pct": None, "wins": 0, "losses": 0},
        )
    template = "memecoins.html" if kind == "market" else f"memecoin_{kind}.html"
    html = web_main.templates.TemplateResponse(
        request=request,
        name=template,
        context=web_main.page_context(request, None, resolved_user=None, **context),
    ).body.decode()
    styles = "\n".join(
        (ROOT / "web/static" / name).read_text()
        for name in (
            "mobile.css",
            "product-system.css",
            "memecoins.css",
        )
    )
    html = html.replace(
        "</head>",
        "<script>localStorage.setItem('rati-release:memecoin-browser-checks', '1');</script>"
        f"<style>{styles}</style></head>",
    )
    html = re.sub(r'<link rel="stylesheet"[^>]*>', "", html)
    script = (ROOT / "web/static/memecoins.js").read_text()
    html = re.sub(
        r'<script src="/static/memecoins.js[^\"]*"[^>]*></script>',
        lambda _: f"<script>{script}</script>",
        html,
    )
    return re.sub(r'<script src="/static/[^\"]*"[^>]*></script>', "", html)


def _open(page: Page, html: str, path: str = "/memecoins/radar?q=doge&sort=gainers") -> None:
    page.route(
        "http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html")
    )
    page.goto(f"http://app.test{path}", wait_until="domcontentloaded")


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_coin_rows_keep_identity_labels_and_browse_context(page: Page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    _open(
        page,
        _html(
            "market",
            signed_in=True,
            market=_market(
                rows=[
                    _coin(),
                    _coin(id="other-doge", name="Other Doge"),
                ]
            ),
        ),
    )
    rows = page.locator("[data-coin-list] > a")
    expect(rows).to_have_count(2)
    expect(rows.first).to_have_attribute(
        "href", "/memecoins/coin/tiny-doge?q=doge&sort=gainers&view=radar"
    )
    expect(rows.nth(1)).to_have_attribute(
        "href", "/memecoins/coin/other-doge?q=doge&sort=gainers&view=radar"
    )
    expect(rows.first).to_have_accessible_name(re.compile("volume \\$0, market cap unknown"))
    expect(rows.first.locator(".quote strong")).to_have_text("$0.00000123")
    expect(rows.first.locator(".quote small")).to_have_text("+0.00%")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("button", name="Open Quiet Signal profile").click()
    expect(page.locator("#profileSheet")).to_be_visible()


def test_refresh_updates_saved_prices_and_preserves_filter_edits(page: Page) -> None:
    _open(page, _html("market"))
    page.get_by_role("searchbox", name="Find a coin").fill("pepe")
    requests: list[str] = []

    def refresh(route: Route) -> None:
        requests.append(route.request.url)
        route.fulfill(json=_market(rows=[_coin(price_label="$0.000002", stale=True)]))

    page.route("**/api/memecoins?**", refresh)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("[data-coin-list] .quote strong")).to_have_text("$0.000002")
    expect(page.get_by_role("searchbox", name="Find a coin")).to_have_value("pepe")
    expect(page.locator(".meme-stale")).to_have_text("Stale")
    assert requests == ["http://app.test/api/memecoins?q=doge&sort=gainers&view=radar"]
    assert "q=doge&sort=gainers" in page.url


@pytest.mark.parametrize(
    "status,title",
    [
        ("pending", "First prices are on the way"),
        ("disabled", "Memecoin feed paused"),
        ("unavailable", "Waiting for pool data"),
        ("ok", "Try another name or symbol"),
    ],
)
def test_saved_feed_empty_states(page: Page, status: str, title: str) -> None:
    _open(page, _html("market", market=_market(rows=[], status=status)))
    expect(page.locator("[data-coin-empty]")).to_be_visible()
    expect(page.locator("[data-coin-empty] strong")).to_have_text(title)


def test_detail_chart_uses_observation_times_and_unknown_metrics(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    _open(page, _html("detail"), "/memecoins/coin/tiny-doge?q=doge&sort=gainers&view=radar")
    chart = page.locator("[data-coin-chart]")
    expect(chart).to_be_visible()
    positions = chart.locator("circle").evaluate_all(
        "nodes => nodes.map(n => +n.getAttribute('cx'))"
    )
    assert positions == pytest.approx([20, 206.666667, 580])
    expect(chart).to_have_accessible_name(re.compile("3 recorded prices"))
    expect(page.locator('[data-coin-metric="high_24h"]')).to_have_text("unknown")
    expect(page.locator('[data-coin-metric="max_supply"]')).to_have_text("0")
    expect(page.get_by_role("link", name="Back to memecoins")).to_have_attribute(
        "href", "/memecoins/radar?q=doge&sort=gainers"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_detail_single_observation_waits_for_a_real_chart(page: Page) -> None:
    _open(
        page,
        _html(
            "detail",
            detail=_detail(history=[{"observed_at": NOW.isoformat(), "price": 0.00000123}]),
        ),
    )
    expect(page.locator("[data-coin-chart]")).to_be_hidden()
    expect(page.locator("[data-chart-status]")).to_contain_text("One saved price")


def test_call_fresh_quote_gate_and_failed_action_recovery(page: Page) -> None:
    _open(page, _html("detail", signed_in=True, detail=_detail(in_current_snapshot=False)))
    action = page.get_by_role("button", name="Make paper Call")
    expect(action).to_be_enabled()
    bodies: list[str] = []

    def make_call(route: Route) -> None:
        bodies.append(route.request.post_data or "")
        route.fulfill(status=409, json={"detail": "A fresh quote is pending. Try again shortly."})

    page.route("**/api/memecoins/tiny-doge/calls", make_call)
    action.click()
    expect(page.locator("[data-call-status]")).to_have_text(
        "A fresh quote is pending. Try again shortly."
    )
    expect(action).to_be_enabled()
    assert json.loads(bodies[0]) == {}
    page.route(
        "**/api/memecoins/tiny-doge",
        lambda route: route.fulfill(
            json=_detail(
                coin=_coin(stale=True),
                can_call=False,
            )
        ),
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(action).to_be_disabled()
    expect(page.locator("[data-call-status]")).to_contain_text("A fresh source quote is required")


def test_alpha_closed_return_and_caller_link_survive_refresh(page: Page) -> None:
    call = _call(
        status="closed", return_pct=12.5, exit_price_label="$0.000001125", exit_at=NOW.isoformat()
    )
    _open(page, _html("alpha", calls=[call]))
    page.route("**/api/memecoin-calls", lambda route: route.fulfill(json={"calls": [call]}))
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("[data-coin-calls] header b")).to_have_text("+12.50%")
    expect(page.get_by_role("link", name="QuietSignal")).to_have_attribute(
        "href", "/u/QuietSignal?market=memecoins"
    )
    expect(page.locator("[data-coin-calls] dl")).to_contain_text("Exit$0.000001125")
    expect(page.locator("[data-calls-empty]")).to_be_hidden()


def test_paper_call_open_and_close_reload_the_current_coin_context(page: Page) -> None:
    current_call: dict[str, Any] | None = None
    posts: list[str] = []
    url = "http://app.test/memecoins/coin/tiny-doge?q=doge&sort=gainers&view=radar"

    def serve(route: Route) -> None:
        route.fulfill(
            content_type="text/html",
            body=_html(
                "detail",
                signed_in=True,
                active_call=current_call,
                calls=[current_call] if current_call else [],
            ),
        )

    def make(route: Route) -> None:
        nonlocal current_call
        posts.append(route.request.url)
        current_call = _call()
        route.fulfill(json={"call": current_call})

    def close(route: Route) -> None:
        nonlocal current_call
        posts.append(route.request.url)
        result = _call(status="closed", exit_at=NOW.isoformat(), exit_price_label="$0.00000123")
        current_call = None
        route.fulfill(json={"call": result})

    page.route("http://app.test/**", serve)
    page.route("**/api/memecoins/tiny-doge/calls", make)
    page.route("**/api/memecoin-calls/call-one/close", close)
    page.goto(url, wait_until="domcontentloaded")
    page.get_by_role("button", name="Make paper Call").click()
    close_button = page.get_by_role("button", name="Close paper Call")
    expect(close_button).to_be_visible()
    assert page.url == url
    expect(page.locator("[data-active-call-return]")).to_have_text("+23.00%")
    close_button.click()
    expect(page.get_by_role("button", name="Make paper Call")).to_be_visible()
    assert page.url == url
    assert posts == [
        "http://app.test/api/memecoins/tiny-doge/calls",
        "http://app.test/api/memecoin-calls/call-one/close",
    ]


def test_automatic_refresh_ages_quote_and_keeps_saved_data_on_error(page: Page) -> None:
    page.clock.install(time=NOW)
    _open(page, _html("detail", signed_in=True))
    requests: list[str] = []

    def failed_refresh(route: Route) -> None:
        requests.append(route.request.url)
        route.fulfill(status=503, json={"detail": "Refresh pending"})

    page.route("**/api/memecoins/tiny-doge", failed_refresh)
    page.clock.fast_forward(16 * 60 * 1000)
    expect(page.get_by_role("button", name="Make paper Call")).to_be_disabled()
    expect(page.locator("[data-coin-price]")).to_have_text("$0.00000123")
    expect(page.locator("[data-detail-status]")).to_contain_text("Saved updates are delayed")
    expect(page.locator("[data-call-status]")).to_contain_text("A fresh source quote is required")
    assert requests


def test_price_chart_keeps_collection_gaps_and_flat_prices(page: Page) -> None:
    history = [
        {"observed_at": (NOW - timedelta(minutes=minute)).isoformat(), "price": 0.1}
        for minute in (30, 25, 0)
    ]
    _open(page, _html("detail", detail=_detail(history=history)))
    chart = page.locator("[data-coin-chart]")
    expect(chart).to_be_visible()
    expect(chart.locator("circle")).to_have_count(3)
    expect(chart.locator("polyline")).to_have_count(1)
    assert chart.locator("circle").evaluate_all(
        "nodes => nodes.map(n => +n.getAttribute('cy'))"
    ) == [110, 110, 110]
    expect(chart).to_have_accessible_name(re.compile("Gaps mark periods"))


def test_stalled_refresh_times_out_and_expires_open_call_marks(page: Page) -> None:
    page.clock.install(time=NOW)
    closed = _call(
        public_id="closed-call",
        status="closed",
        return_pct=10,
        exit_at=NOW.isoformat(),
        exit_price_label="$0.0000011",
    )
    _open(page, _html("detail", signed_in=True, active_call=_call(), calls=[_call(), closed]))
    held: list[Route] = []
    page.route("**/api/memecoins/tiny-doge", lambda route: held.append(route))
    page.clock.fast_forward(16 * 60 * 1000)
    expect(page.locator("[data-active-call-return]")).to_have_text("Pending")
    expect(page.locator('[data-call-id="call-one"] header b')).to_have_text("Pending")
    expect(page.locator('[data-call-id="closed-call"] header b')).to_have_text("+10.00%")
    expect(page.get_by_role("button", name="Close paper Call")).to_be_disabled()
    expect(page.get_by_role("button", name="Refreshing…")).to_be_disabled()
    page.clock.fast_forward(11000)
    expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_enabled()
    expect(page.locator("[data-detail-status]")).to_contain_text("Saved updates are delayed")
    assert held
    page.route(
        "**/api/memecoins/tiny-doge",
        lambda route: route.fulfill(
            json=_detail(
                coin=_coin(observed_at=(NOW + timedelta(minutes=16)).isoformat()),
            )
        ),
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.get_by_role("button", name="Close paper Call")).to_be_enabled()
    expect(page.locator("[data-coin-price]")).to_have_text("$0.00000123")


def test_future_source_time_uses_the_server_freshness_tolerance(page: Page) -> None:
    page.clock.install(time=NOW)
    future = (NOW + timedelta(seconds=90)).isoformat()
    _open(page, _html("detail", signed_in=True, detail=_detail(coin=_coin(observed_at=future))))
    expect(page.get_by_role("button", name="Make paper Call")).to_be_disabled()
    expect(page.locator("[data-detail-status]")).to_contain_text("Waiting for a fresh source time")


@pytest.mark.parametrize("view", ["pulse", "radar"])
def test_refresh_preserves_market_view_and_applied_filters(page: Page, view: str) -> None:
    path = "/memecoins" if view == "pulse" else "/memecoins/radar"
    _open(
        page,
        _html("market", market=_market(view=view), list_path=path),
        f"{path}?q=doge&sort=gainers",
    )
    requests: list[str] = []

    def refresh(route: Route) -> None:
        requests.append(route.request.url)
        route.fulfill(json=_market(view=view, rows=[_coin(price_label="$0.000002")]))

    page.route("**/api/memecoins?**", refresh)
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("[data-coin-list] .quote strong")).to_have_text("$0.000002")
    expect(page.get_by_role("searchbox", name="Find a coin")).to_have_value("doge")
    expect(page.get_by_role("combobox", name="Sort by")).to_have_value("gainers")
    expect(page.locator("[data-coin-list] > a")).to_have_attribute(
        "href", f"/memecoins/coin/tiny-doge?q=doge&sort=gainers&view={view}"
    )
    expect(page.locator("[data-market-scope]")).to_have_text(
        "Fresh quotes · up to 20 coins" if view == "pulse" else "Helius pool discoveries"
    )
    assert requests == [f"http://app.test/api/memecoins?q=doge&sort=gainers&view={view}"]
    assert page.url == f"http://app.test{path}?q=doge&sort=gainers"


def test_stale_pulse_links_to_saved_radar_prices(page: Page) -> None:
    _open(
        page,
        _html(
            "market", market=_market(view="pulse", rows=[], status="stale"), list_path="/memecoins"
        ),
    )
    expect(page.locator("[data-coin-empty] strong")).to_have_text("Waiting for fresh quotes")
    expect(page.get_by_role("link", name="View saved prices in Radar")).to_have_attribute(
        "href", "/memecoins/radar?q=doge&sort=gainers"
    )


def test_chain_pool_evidence_on_mobile(page: Page) -> None:
    page.set_viewport_size({"width": 320, "height": 850})
    address = "AbC" * 14
    coin = _coin(
        network="solana",
        token_address=address,
        pool_address="Pool123",
        liquidity_usd=2345,
        buys_24h=4,
        sells_24h=2,
        time_basis="indexer_fetch",
        name="solana · " + address,
    )
    _open(
        page,
        _html("detail", detail=_detail(coin=coin, source="GeckoTerminal")),
        "/memecoins/coin/tiny-doge",
    )
    expect(page.get_by_text(address, exact=True)).to_be_visible()
    expect(page.get_by_text("4 buys · 2 sells", exact=True)).to_be_visible()
    expect(
        page.get_by_text("Quote time records when GeckoTerminal returned the pool data.")
    ).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    updated = {
        **coin,
        "pool_address": "DeeperPool",
        "buys_24h": 8,
        "source_url": "https://www.geckoterminal.com/solana/pools/DeeperPool",
    }
    page.route(
        "**/api/memecoins/tiny-doge",
        lambda route: route.fulfill(json=_detail(coin=updated, source="GeckoTerminal")),
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("[data-pool-address]")).to_have_text("DeeperPool")
    expect(page.locator("[data-pool-trades]")).to_have_text("8 buys · 2 sells")
    expect(page.locator("[data-pool-source]")).to_have_attribute("href", updated["source_url"])


def test_empty_chain_window_has_clear_copy(page: Page) -> None:
    market = _market(rows=[], total=0)
    _open(page, _html("market", market=market))
    expect(page.locator("[data-coin-empty] strong")).to_have_text("Waiting for active pools")
    page.route("**/api/memecoins?**", lambda route: route.fulfill(json=market))
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("[data-coin-empty] strong")).to_have_text("Waiting for active pools")


def test_on_chain_watch_shows_both_receipts_and_refreshes(page: Page) -> None:
    page.set_viewport_size({"width": 320, "height": 850})
    alert = {
        "title": "Pool-linked wallet sold",
        "net_token_amount": "6.000000",
        "wallet": "AbC" * 14,
        "token_address": "Def" * 14,
        "observed_at": NOW.isoformat(),
        "role_label": "Declared coin creator",
        "source_url": "https://solscan.io/tx/trade",
        "relationship_source_url": "https://solscan.io/tx/creation",
    }
    market = _market(
        rows=[],
        total=0,
        integrity_alerts=[alert],
        integrity_coverage={"checked_at": NOW.isoformat()},
    )
    _open(page, _html("market", market=market))
    expect(page.locator("[data-integrity-alerts]")).to_contain_text("Declared coin creator")
    expect(page.get_by_role("link", name="Pool creation link ↗")).to_have_attribute(
        "href", alert["relationship_source_url"]
    )
    expect(
        page.get_by_role("link", name="Pool-linked wallet sold · 6.000000 tokens ↗")
    ).to_have_attribute("href", alert["source_url"])
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    updated = {**market, "integrity_alerts": []}
    page.route("**/api/memecoins?**", lambda route: route.fulfill(json=updated))
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("[data-integrity-alerts] li")).to_have_count(0)
    expect(page.locator("[data-integrity-empty]")).to_be_visible()


def test_pattern_findings_show_basis_evidence_and_credit_budget(page: Page) -> None:
    alert = {
        "title": "Buyers share a SOL funding source",
        "basis": "relationship",
        "wallet": "root",
        "token_address": "mint",
        "observed_at": NOW.isoformat(),
        "source_url": "https://solscan.io/tx/buy",
        "related_wallets": ["wallet-a", "wallet-b"],
        "explanation": "Shared services can also create this funding pattern.",
        "evidence": [{"kind": "sol_transfer", "source_url": "https://solscan.io/tx/fund"}],
    }
    market = _market(
        integrity_alerts=[alert],
        integrity_coverage={
            "checked_at": NOW.isoformat(),
            "budget": {"reserved_credits": 8640, "daily_limit": 10000},
            "recorded_coverage_gaps": 2,
        },
    )
    _open(page, _html("market", market=market))
    expect(page.locator("[data-integrity-alerts]")).to_contain_text("wallet-a · wallet-b")
    expect(page.locator("[data-integrity-alerts]")).to_contain_text(alert["explanation"])
    expect(page.locator("[data-integrity-gaps]")).to_contain_text("2 recorded coverage gaps")
    expect(page.locator("[data-integrity-budget]")).to_have_text(
        "8640 / 10000 credits reserved today"
    )
    page.route("**/api/memecoins?**", lambda route: route.fulfill(json=market))
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("[data-integrity-alerts]")).to_contain_text("relationship")
    expect(page.get_by_role("link", name="sol_transfer ↗")).to_have_attribute(
        "href", "https://solscan.io/tx/fund"
    )
    expect(page.locator("[data-integrity-alerts]")).not_to_contain_text("undefined")


@pytest.mark.parametrize(
    "unsafe_url", ["javascript:alert(1)", "data:text/html,<script>alert(1)</script>"]
)
def test_refreshed_evidence_links_require_solscan_transactions(page: Page, unsafe_url: str) -> None:
    market = _market(
        integrity_alerts=[
            {
                "title": "Observed trade",
                "wallet": "wallet",
                "token_address": "mint",
                "observed_at": NOW.isoformat(),
                "source_url": unsafe_url,
                "relationship_source_url": unsafe_url,
                "evidence": [{"kind": "proof", "source_url": unsafe_url}],
            }
        ]
    )
    _open(page, _html("market", market=_market()))
    page.route("**/api/memecoins?**", lambda route: route.fulfill(json=market))
    page.get_by_role("button", name="Refresh", exact=True).click()
    links = page.locator("[data-integrity-alerts] a")
    expect(links).to_have_count(3)
    for link in links.all():
        expect(link).to_have_attribute("href", "#")
