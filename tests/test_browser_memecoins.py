from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect
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
