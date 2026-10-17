from __future__ import annotations

import mimetypes
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from starlette.requests import Request

from runner_web import api_routes, db
from runner_web import main as web
from tests.test_metered_api import api  # noqa: F401

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("width,height", [(390, 844), (1280, 900)])
def test_developer_keys_and_mobile_layout(page: Page, api, monkeypatch, width, height):  # noqa: F811
    client, _ = api
    with db.connection() as database:
        user = dict(database.execute("SELECT * FROM users WHERE id='alice'").fetchone())
    monkeypatch.setattr(web, "current_user", lambda _: user)
    request = Request(
        {
            "type": "http",
            "path": "/developers",
            "method": "GET",
            "query_string": b"",
            "headers": [],
            "scheme": "https",
            "server": ("example.test", 443),
        }
    )
    request.state.csp_nonce = "browser-test"
    html = web.developer_api_page(request, None, api_routes.pricing()).body.decode()

    def route_handler(route):
        from urllib.parse import urlparse

        path = urlparse(route.request.url).path
        if path == "/developers":
            return route.fulfill(content_type="text/html", body=html)
        if path.startswith("/static/"):
            file = ROOT / "web" / path.lstrip("/")
            if file.is_file():
                return route.fulfill(
                    body=file.read_bytes(),
                    content_type=mimetypes.guess_type(file)[0] or "text/plain",
                )
        if path.startswith("/api/developer/"):
            response = client.request(
                route.request.method,
                path,
                content=route.request.post_data,
                headers={"Origin": "https://example.test", "Content-Type": "application/json"},
            )
            return route.fulfill(
                status=response.status_code, content_type="application/json", body=response.content
            )
        return route.fulfill(status=404, body="")

    page.route("https://example.test/**", route_handler)
    page.set_viewport_size({"width": width, "height": height})
    page.goto("https://example.test/developers")
    expect(page.get_by_role("heading", name="Runner Watch API", exact=True)).to_be_visible()
    expect(page.locator("#flashReleaseDialog")).not_to_be_visible()
    expect(page.locator("#apiBalance")).to_contain_text("100 free credits")
    page.get_by_label("Key name").fill("My bot")
    page.get_by_role("button", name="Create API key").click()
    expect(page.locator("#newApiKey")).to_contain_text("rw_")
    expect(page.locator("#apiKeys")).to_contain_text("My bot")
    key_row = page.locator("#apiKeys li").filter(has_text="My bot")
    key_row.get_by_role("button", name="Revoke").click()
    expect(page.locator("#apiKeys")).not_to_contain_text("My bot")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator("#apiError").inner_text() == ""
    # Keep generated credentials out of the retained screenshot.
    page.locator("#newApiKey").evaluate(
        '(element) => element.textContent = "Key saved to your password manager"'
    )
    page.locator("h1").scroll_into_view_if_needed()
    output = ROOT / "artifacts" / "developer-api"
    output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f"developer-{width}.png"), full_page=True)
