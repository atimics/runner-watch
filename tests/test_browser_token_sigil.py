"""The existing token Sigil holds first-read facts, pool state, rewards and KOL PnL."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, expect
from starlette.requests import Request

from runner_web import main
from runner_web.market_screens import detail as screen_detail
from runner_web.token_sigil import pending_detail
from tests.test_token_discoveries import MINT

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parents[1]


def open_sigil(page, *, signed_in=False, rejected=False, name="Sample token"):
    basics = {
        "address": MINT,
        "status": "ready",
        "name": name,
        "symbol": "SAMPLE",
        "checked_at": "2026-10-10T12:00:00Z",
        "facts": [
            {"label": "Supply", "value": "18446744073.709551615"},
            {"label": "Mint authority", "value": "Revoked"},
            {"label": "Freeze authority", "value": MINT},
            {"label": "Metadata edits", "value": "Locked"},
        ],
    }
    detail = pending_detail(MINT, basics)
    detail.update(
        token_lookup=basics,
        discovery={
            "mint": MINT,
            "status": "rejected" if rejected else "review",
            "met": 3,
            "total": 9,
            "reward_status": "available",
            "reward_amount": 50,
            "checked_at": None,
            "failed_checks": [{"label": "Top ten holders", "detail": "40%"}] if rejected else [],
            "proof_url": "/api/memecoin-rejections/0" if rejected else None,
            "rejection_tree": {"tree_size": 1, "root_hash": "ab" * 32},
        },
        token_positions=[
            {
                "name": "Sample KOL",
                "address": MINT,
                "quantity": 1000,
                "value_usd": 20,
                "unrealized": 1.5,
                "unit": "SOL",
                "updated_at": "2026-10-10T12:00:00Z",
            }
        ],
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/memecoins/coin/" + MINT,
            "query_string": b"",
            "headers": [(b"host", b"app.test")],
            "scheme": "http",
            "server": ("app.test", 80),
        }
    )
    request.state.csp_nonce = "test"
    html = main.templates.TemplateResponse(
        request,
        "simple_coin_detail.html",
        {
            "request": request,
            "detail": detail,
            "calls": [],
            "active_call": None,
            "user": {"id": "alice"} if signed_in else None,
            "runners_origin": "http://app.test",
            "sports_origin": "http://sports.test",
            "static_version": "test",
        },
    ).body.decode()
    screen = screen_detail("memecoins", detail)

    def serve(route):
        path = urlsplit(route.request.url).path
        if path.startswith("/static/"):
            file = ROOT / "web/static" / path.rsplit("/", 1)[-1]
            if file.is_file():
                route.fulfill(path=str(file))
            else:
                route.abort()
        elif path.startswith("/api/screens/"):
            route.fulfill(json=screen)
        elif path.startswith("/api/memecoin-discoveries/"):
            route.fulfill(json={"reward_status": "reserved", "reward_amount": 50})
        elif path.startswith("/api/"):
            route.fulfill(json={})
        else:
            route.fulfill(content_type="text/html", body=html)

    page.route("**/*", serve)
    page.goto("http://app.test/memecoins/coin/" + MINT)


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_first_read_fits_the_existing_sigil_with_checks_and_wallet_pnl(page: Page, width, tmp_path):
    page.set_viewport_size({"width": width, "height": 844})
    open_sigil(page, rejected=True)
    expect(page.get_by_role("region", name="Token Sigil")).to_be_visible()
    assert page.locator(".token-sigil-glyph .well-glyph").bounding_box()["width"] >= 180
    expect(page.locator("[data-facts]")).to_contain_text("18446744073.709551615")
    expect(page.locator("[data-facts]")).to_contain_text("Locked")
    expect(page.get_by_role("heading", name="Rejected pool")).to_be_visible()
    page.get_by_text("Saved rejection receipt", exact=True).click()
    expect(page.get_by_role("link", name="Download receipt")).to_be_visible()
    page.get_by_text("Sample KOL · 1000 tokens", exact=True).click()
    expect(page.get_by_role("region", name="KOL positions in this token")).to_contain_text(
        "+1.5000 SOL"
    )
    expect(page.locator('[href="/wallets"]')).to_have_count(0)
    expect(page.locator("[data-token-lookup]")).to_have_count(0)
    search = page.get_by_role("combobox")
    search.focus()
    page.set_viewport_size({"width": width, "height": 500})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": width, "height": 844})
    search.blur()
    page.screenshot(path=str(tmp_path / f"token-sigil-{width}.png"), full_page=True)


def test_submission_saves_on_the_same_sigil_and_displays_the_reward(page: Page):
    open_sigil(page, signed_in=True)
    page.get_by_role("button", name="Submit for ratification").click()
    expect(page.locator("[data-submission-status]")).to_have_text("Submission saved.")
    expect(page.locator("[data-discovery-reward]")).to_have_text(
        "You earn 50 Flash when all ratification checks pass."
    )
    expect(page.get_by_role("button", name="Submit for ratification")).to_be_hidden()
    expect(page).to_have_url("http://app.test/memecoins/coin/" + MINT)


def test_creator_text_stays_escaped_inside_the_sigil_facts(page: Page):
    name = '<img src=x onerror="window.sigilScript=true">'
    open_sigil(page, name=name)
    expect(page.locator("[data-facts]")).to_contain_text(name)
    assert page.evaluate("window.sigilScript === undefined")
