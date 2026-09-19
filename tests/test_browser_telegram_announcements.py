import json
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from runner_web import main
from tests.test_browser_ticker import _request

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("width", [390, 1280])
def test_channel_preview_handles_access_receipts_and_untrusted_text(page: Page, width):
    request = _request()
    html = main.templates.TemplateResponse(
        request, "telegram_announcements.html", main.page_context(request, None, resolved_user=None)
    ).body.decode()
    seen = []

    def route(request_route):
        req = request_route.request
        if "/api/telegram/announcements" in req.url:
            seen.append(req.headers.get("authorization"))
            if req.headers.get("authorization") != "Bearer fixture-key":
                request_route.fulfill(status=404, body="{}", content_type="application/json")
            else:
                request_route.fulfill(
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "limits": {
                                "daily": 24,
                                "interval_seconds": 300,
                                "ticker_quiet_seconds": 1800,
                            },
                            "posts": [
                                {
                                    "status": "sent",
                                    "format": "text",
                                    "chat_id": "fixture-chat",
                                    "text": (
                                        "$TEST · Bought\nJane Lee · 100 common shares\n"
                                        "<script>alert(1)</script>"
                                    ),
                                    "updated_at": "2026-09-13T02:00:00+00:00",
                                    "message_id": 42,
                                    "attempts": 1,
                                },
                                {
                                    "status": "uncertain",
                                    "format": "animation",
                                    "chat_id": "fixture-chat",
                                    "text": "New memecoin detected · TEST",
                                    "updated_at": "2026-09-13T01:00:00+00:00",
                                },
                            ],
                        }
                    ),
                )
        elif "/static/" in req.url:
            name = req.url.split("/static/")[1].split("?")[0]
            path = ROOT / "web" / "static" / name
            if path.is_file():
                kind = (
                    "text/css"
                    if name.endswith(".css")
                    else "application/javascript"
                    if name.endswith(".js")
                    else "image/svg+xml"
                )
                request_route.fulfill(body=path.read_bytes(), content_type=kind)
            else:
                request_route.fulfill(status=404)
        elif req.is_navigation_request():
            request_route.fulfill(body=html, content_type="text/html")
        else:
            request_route.fulfill(status=404)

    page.route("**/*", route)
    page.set_viewport_size({"width": width, "height": 900})
    page.goto("https://runners.rati.chat/telegram/announcements")
    expect(page.get_by_role("heading", name="Channel posts")).to_be_visible()
    page.get_by_label("Operations access key").fill("wrong")
    page.get_by_role("button", name="Open history").click()
    expect(page.get_by_role("status")).to_have_text("Check your access key and try again.")
    page.get_by_label("Operations access key").fill("fixture-key")
    page.get_by_role("button", name="Open history").click()
    expect(page.get_by_role("status")).to_have_text("2 recent posts")
    expect(page.get_by_text("Telegram receipt 42 · 1 attempt")).to_be_visible()
    expect(page.get_by_text("Check the chat to confirm delivery.", exact=False)).to_be_visible()
    expect(page.locator(".channel-post script")).to_have_count(0)
    assert seen == ["Bearer wrong", "Bearer fixture-key"]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert page.evaluate("localStorage.length") == 0
