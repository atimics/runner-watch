"""Wallet PnL, cost coverage, and refresh actions in the rendered page."""

import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, expect
from starlette.requests import Request

from runner_web import main
from runner_web.onchain_wallets import holdings_page, wallet_label
from runner_web.wallet_registry import register_chain
from tests.test_onchain_wallets import ADDRESS, AT, sample_pnl
from tests.test_onchain_wallets import database as database

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parents[1]


def open_wallet(
    page: Page,
    *,
    width=390,
    pending=False,
    unknown=False,
    large=False,
    backfilling=False,
    ready_on_reload=False,
):
    page.set_viewport_size({"width": width, "height": 900})
    wallet_id = register_chain(ADDRESS)
    wallet = {
        "address": ADDRESS,
        "label": wallet_label(ADDRESS),
        "status": "pending" if pending else "ready",
        "provider_ready": pending,
        "error": None,
    }
    if not pending:
        wallet.update(
            **sample_pnl(),
            updated_at=AT.isoformat(),
            balance_sol=8,
            has_more=True,
            commitment="finalized",
            history_hash="test-history",
        )
    if unknown:
        wallet["pnl"][0].update(realized=None, unrealized=None, closed_trades=0, priced_positions=0)
        wallet["holdings"][0].update(cost=None, unrealized=None)
        wallet.update(unknown_sales=1, unknown_holdings=1)
    if large:
        row = wallet["holdings"][0]
        wallet["holdings"] = [row] + [{**row, "name": f"Token {index}"} for index in range(1, 2627)]
    if backfilling:
        wallet.update(provider_ready=True, backfill={"status": "loading"})
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/wallet/" + wallet_id,
            "headers": [(b"host", b"app.test")],
            "scheme": "http",
            "server": ("app.test", 80),
            "query_string": b"",
        }
    )
    request.state.csp_nonce = "browser-test"

    loads = 0

    def render(route):
        nonlocal loads
        loads += 1
        if ready_on_reload and loads > 1:
            wallet.update(
                updated_at=(AT + timedelta(minutes=1)).isoformat(),
                backfill={"status": "complete"},
                has_more=False,
                transactions=4,
            )
        query = parse_qs(urlsplit(route.request.url).query)
        number = int(query.get("holdings_page", ["1"])[0])
        html = main.templates.TemplateResponse(
            request,
            "onchain_wallet.html",
            main.page_context(
                request,
                None,
                resolved_user=None,
                nav_product="memecoins",
                wallet=holdings_page(wallet, number),
                wallet_id=wallet_id,
                screen={"kind": "memecoins"},
            ),
        ).body.decode()
        html = re.sub(
            r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
            lambda match: "<style>" + (ROOT / "web/static" / match[1]).read_text() + "</style>",
            html,
        )
        html = re.sub(
            r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>',
            lambda match: "<script>" + (ROOT / "web/static" / match[1]).read_text() + "</script>",
            html,
        )
        route.fulfill(body=html, content_type="text/html")

    page.route("http://app.test/**", render)
    return wallet_id


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_wallet_pnl_balances_and_receipts_fit_the_screen(page: Page, database, width):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    wallet_id = open_wallet(page, width=width)
    page.goto("http://app.test/wallet/" + wallet_id)
    pnl = page.get_by_role("region", name="Profit and loss")
    expect(pnl).to_contain_text("+0.5000 SOL")
    expect(pnl).to_contain_text("+1.5000 SOL")
    expect(page.get_by_role("region", name="Wallet balances")).to_contain_text("0.00001500 SOL")
    expect(page.get_by_role("region", name="Token holdings")).to_contain_text("$600.00")
    expect(page.get_by_role("region", name="Wallet trades").get_by_role("link")).to_have_count(3)
    expect(page.locator(".chain-coverage").first).to_contain_text("History backfill in progress")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_unknown_costs_render_pending(page: Page, database):
    wallet_id = open_wallet(page, unknown=True)
    page.goto("http://app.test/wallet/" + wallet_id)
    cards = page.get_by_role("region", name="Profit and loss").locator("article")
    expect(cards.nth(0).locator("strong")).to_have_text("Pending")
    expect(cards.nth(1).locator("strong")).to_have_text("Pending")
    expect(page.get_by_role("region", name="Token holdings")).to_contain_text("Pending")


@pytest.mark.parametrize("width", [390, 1440])
def test_large_wallet_holdings_pages_keep_pnl_and_fit_the_screen(page: Page, database, width):
    wallet_id = open_wallet(page, width=width, large=True)
    page.goto("http://app.test/wallet/" + wallet_id)
    holdings = page.get_by_role("region", name="Token holdings")
    expect(holdings).to_contain_text("2627 tokens · Page 1 of 27")
    expect(holdings.locator("tbody tr")).to_have_count(100)
    link = page.get_by_role("link", name="Next holdings")
    link.focus()
    link.press("Enter")
    expect(holdings).to_contain_text("2627 tokens · Page 2 of 27")
    expect(holdings.locator("tbody tr")).to_have_count(100)
    expect(holdings.locator("tbody tr").first).to_contain_text("Token 100")
    expect(page.get_by_role("region", name="Profit and loss")).to_contain_text("+0.5000 SOL")
    expect(page.get_by_role("link", name="Previous holdings")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_failed_refresh_preserves_saved_pnl_and_allows_retry(page: Page, database):
    wallet_id = open_wallet(page)
    requests = []

    def refresh(route):
        requests.append(route.request.method)
        route.fulfill(json={"status": "ready", "error": "Try again in a minute."})

    page.route("**/api/wallets/*/refresh", refresh)
    page.goto("http://app.test/wallet/" + wallet_id)
    button = page.get_by_role("button", name="Refresh chain data")
    button.focus()
    button.press("Enter")
    expect(page.locator("[data-wallet-status]")).to_have_text("Try again in a minute.")
    expect(button).to_be_enabled()
    expect(page.get_by_role("region", name="Profit and loss")).to_contain_text("+0.5000 SOL")
    assert requests == ["POST"]


@pytest.mark.parametrize("width", [390, 1440])
def test_backfill_progress_reads_saved_data_and_updates_the_page(page: Page, database, width):
    page.clock.install()
    wallet_id = open_wallet(page, width=width, backfilling=True, ready_on_reload=True)
    reads = []

    def progress(route):
        reads.append(route.request.method)
        route.fulfill(
            json={
                "status": "ready",
                "error": None,
                "updated_at": (AT + timedelta(minutes=1)).isoformat(),
                "backfill": {"status": "complete"},
            }
        )

    page.route("**/api/wallets/*/pnl?summary=true", progress)
    page.goto("http://app.test/wallet/" + wallet_id)
    expect(page.get_by_role("button", name="Load more history")).to_be_visible()
    expect(page.locator(".chain-coverage").first).to_contain_text("continues in the background")
    page.clock.fast_forward(15001)
    expect(page.locator(".chain-coverage").first).to_contain_text("4 transactions loaded")
    expect(page.locator(".chain-coverage").first).to_contain_text("History caught up")
    expect(page.get_by_role("button", name="Refresh chain data")).to_be_visible()
    assert reads == ["GET"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_first_read_starts_when_provider_is_ready(page: Page, database):
    wallet_id = open_wallet(page, pending=True)
    requests = []

    def refresh(route):
        requests.append(route.request.method)
        route.fulfill(json={"status": "pending", "error": "Try again in a minute."})

    page.route("**/api/wallets/*/refresh", refresh)
    page.goto("http://app.test/wallet/" + wallet_id)
    expect(page.locator("[data-wallet-status]")).to_have_text("Try again in a minute.")
    expect(page.get_by_role("button", name="Load chain data")).to_be_enabled()
    assert requests == ["POST"]
