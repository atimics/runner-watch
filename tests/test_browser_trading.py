from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Page, expect

from runner_trading import ledger
from runner_trading.config import stamp
from runner_web.db import connection
from tests.test_trading import setup_trader
from tests.test_trading_routes import client as client
from tests.test_trading_routes import trading_db as trading_db

pytestmark = pytest.mark.browser


def open_desk(page, client, width):
    from datetime import timedelta

    trader, clock, source, provider = setup_trader()
    trader.cycle()
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    with connection() as db:
        db.execute("UPDATE trading_accounts SET heartbeat_at=?", (stamp(),))
    client.cookies.set("runner_session", "operator")
    page.set_viewport_size({"width": width, "height": 900})

    def respond(route):
        request = route.request
        parts = urlsplit(request.url)
        path = parts.path + ("?" + parts.query if parts.query else "")
        response = client.request(
            request.method,
            path,
            content=request.post_data,
            headers={
                "Origin": "http://testserver",
                "Content-Type": request.headers.get("content-type", ""),
            },
        )
        route.fulfill(
            status=response.status_code,
            body=response.content,
            headers={
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"content-length", "content-encoding"}
            },
        )

    page.route("http://testserver/**", respond)
    page.goto("http://testserver/trading")
    return trader


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_trading_desk_fits_and_exit_dialog_works_with_keyboard(page: Page, client, width):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    trader = open_desk(page, client, width)
    expect(page.get_by_role("heading", name="Trading desk")).to_be_visible()
    expect(page.get_by_text("Virtual funds", exact=True)).to_be_visible()
    page.screenshot(path=str(Path("/private/tmp") / f"solana-trading-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        page.evaluate(
            "[...document.querySelectorAll('body *')].filter(e => "
            "e.getBoundingClientRect().right > "
            "innerWidth && getComputedStyle(e).position !== 'fixed').slice(0,12).map(e=>"
            "[e.tagName,e.className,e.getBoundingClientRect().right])"
        )
    )
    page.get_by_role("button", name="Request exit", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    expect(page.get_by_role("button", name="Keep open")).to_be_focused()
    page.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(page.get_by_role("button", name="Request exit", exact=True)).to_be_focused()
    page.get_by_role("button", name="Request exit", exact=True).click()
    dialog.get_by_role("button", name="Request exit", exact=True).click()
    expect(page.get_by_role("button", name="Request exit", exact=True)).to_be_disabled()
    assert (
        ledger.rows("trading_positions", trader.identifier, active=True)[0]["exit_requested"] == 1
    )
    assert errors == []
    page.screenshot(path=str(Path("/private/tmp") / f"solana-trading-{width}.png"), full_page=True)


def test_operator_can_pause_and_resume_the_paper_account(page: Page, client):
    trader = open_desk(page, client, 390)
    page.get_by_role("button", name="Pause entries").click()
    expect(page.get_by_role("button", name="Resume entries")).to_be_visible()
    assert ledger.account(trader.identifier)["paused"] == 1
    page.get_by_role("button", name="Resume entries").click()
    expect(page.get_by_role("button", name="Pause entries")).to_be_visible()
    assert ledger.account(trader.identifier)["paused"] == 0
