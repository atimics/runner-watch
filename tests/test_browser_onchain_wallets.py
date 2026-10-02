"""Wallet PnL, cost coverage, and refresh actions in the rendered page."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from starlette.requests import Request

from runner_web import main
from runner_web.onchain_wallets import wallet_label
from runner_web.wallet_registry import register_chain
from tests.test_onchain_wallets import ADDRESS, AT, sample_pnl
from tests.test_onchain_wallets import database as database

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parents[1]


def open_wallet(page: Page, *, width=390, pending=False, unknown=False):
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
    html = main.templates.TemplateResponse(
        request,
        "onchain_wallet.html",
        main.page_context(
            request,
            None,
            resolved_user=None,
            nav_product="memecoins",
            wallet=wallet,
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
    page.route(
        "http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html")
    )
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
    expect(page.locator(".chain-coverage").first).to_contain_text("Older history available")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


def test_unknown_costs_render_pending(page: Page, database):
    wallet_id = open_wallet(page, unknown=True)
    page.goto("http://app.test/wallet/" + wallet_id)
    cards = page.get_by_role("region", name="Profit and loss").locator("article")
    expect(cards.nth(0).locator("strong")).to_have_text("Pending")
    expect(cards.nth(1).locator("strong")).to_have_text("Pending")
    expect(page.get_by_role("region", name="Token holdings")).to_contain_text("Pending")


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
