import json
from pathlib import Path

import pytest
from playwright.sync_api import Page

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parents[1]


def _open_reporter(page: Page) -> None:
    reporter = (ROOT / "web/static/client-errors.js").read_text()
    html = (
        "<!doctype html><html><head><script>"
        + reporter
        + "</script></head><body>ready</body></html>"
    )
    page.route(
        "http://app.test/**", lambda route: route.fulfill(content_type="text/html", body=html)
    )
    page.route("**/api/client-errors", lambda route: route.fulfill(status=204))
    page.goto("http://app.test/boom")


def test_uncaught_error_is_reported_with_page_and_message(page: Page) -> None:
    _open_reporter(page)

    with page.expect_request("**/api/client-errors") as captured:
        page.evaluate("setTimeout(() => { throw new Error('kaboom from the page'); }, 0)")

    payload = json.loads(captured.value.post_data)
    assert payload["kind"] == "error"
    assert "kaboom from the page" in payload["message"]
    assert payload["page_url"] == "/boom"


def test_unhandled_rejection_is_reported(page: Page) -> None:
    _open_reporter(page)

    with page.expect_request("**/api/client-errors") as captured:
        page.evaluate(
            "setTimeout(() => { Promise.reject(new Error('rejected by the page')); }, 0)"
        )

    payload = json.loads(captured.value.post_data)
    assert payload["kind"] == "rejection"
    assert "rejected by the page" in payload["message"]
