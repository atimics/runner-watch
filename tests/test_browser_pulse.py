"""The stock entry point uses the shared market screen."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from runner_web import main as web

pytestmark = pytest.mark.browser


def test_stock_home_renders_the_shared_screen(page: Page, monkeypatch):
    from starlette.requests import Request

    monkeypatch.setattr(web, "current_user", lambda *_: None)
    monkeypatch.setattr(
        web,
        "page_context",
        lambda request, session, **values: {
            "request": request,
            "user": None,
            "runners_origin": "http://app.test",
            "sports_origin": "http://sports.test",
            **values,
        },
    )
    monkeypatch.setattr(
        web,
        "_public_pulse_data",
        lambda **_: {
            "rows": [{"ticker": "ONE", "company": "One Company", "price": 1.25, "change_pct": 4.2}]
        },
    )
    request = Request(
        {
            "type": "http",
            "path": "/",
            "method": "GET",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("app.test", 80),
        }
    )
    request.state.csp_nonce = "test"
    html = web.home(request, None).body.decode()
    page.route("http://app.test/", lambda r: r.fulfill(content_type="text/html", body=html))
    page.goto("http://app.test/")
    expect(page.get_by_role("heading", name="List", exact=True)).to_be_visible()
    expect(page.locator(".ticker").filter(has_text="ONE")).to_have_attribute(
        "href", "/t/ONE"
    )
    expect(page.get_by_role("navigation", name="View").get_by_role("link")).to_have_count(2)
