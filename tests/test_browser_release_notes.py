from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page
from starlette.requests import Request

from runner_web import main as web_main

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/billing",
            "headers": [(b"host", b"runners.rati.chat")],
            "scheme": "https",
            "server": ("runners.rati.chat", 443),
            "client": ("127.0.0.1", 1234),
            "query_string": b"",
        }
    )
    request.state.csp_nonce = "browser-test"
    return request


def _rendered_release_notes() -> str:
    response = web_main.templates.TemplateResponse(
        request=_request(),
        name="_flash_release_modal.html",
        context={
            "user": {"id": "release-browser-user"},
            "release_announcement_id": "browser-release",
            "flash_wallet": {
                "balance": 170,
                "can_claim": True,
                "claim_day": "2026-08-29",
            },
        },
    )
    styles = (ROOT / "web/static/product-system.css").read_text()
    return f"""
      <!doctype html>
      <html>
        <head>
          <style>
            :root {{ --text: #f1f4f2; --muted: #8e9992; --soft: #c5ccc7; --line: #263129; }}
            body {{ margin: 0; background: #080b09; font-family: Arial, sans-serif; }}
            {styles}
          </style>
        </head>
        <body>{response.body.decode()}</body>
      </html>
    """


@pytest.mark.parametrize("width", [390, 1280])
def test_release_notes_stay_compact_and_factual(page: Page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    errors: list[BaseException] = []
    page.on("pageerror", lambda error: errors.append(error))
    page.route(
        "http://app.test/release",
        lambda route: route.fulfill(
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=_rendered_release_notes(),
        ),
    )
    page.goto("http://app.test/release", wait_until="domcontentloaded")

    dialog = page.locator("#flashReleaseDialog")
    dialog.evaluate("element => element.showModal()")
    dialog.wait_for(state="visible")
    box = dialog.bounding_box()
    assert box is not None
    assert box["width"] <= 460
    assert box["height"] < 430
    assert page.locator(".flash-release-visual").count() == 0
    assert page.locator(".flash-release-features li").all_inner_texts() == [
        "Flash balance\nOpen the wallet or claim 100 daily Flash.",
        "Caller PnL\nAverage closed-Call return and record.",
        "Rewards\nProfitable stock Calls earn 10× their return in Flash.",
    ]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
    assert errors == []
