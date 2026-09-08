from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from runner_web import main as web_main

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser


def _notice(**values: Any) -> dict[str, Any]:
    return {
        "id": 1,
        "kind": "holdings",
        "label": "Holdings",
        "text": "I hold shares in this company.",
        "reason": "",
        "created_at": "2026-09-05T18:00:00+00:00",
        "recorded_by": "author",
        **values,
    }


def _open(page: Page, *, disclosures: list | None = None, corrections: list | None = None) -> None:
    page.set_default_timeout(5000)
    template = web_main.templates.env.from_string(
        '{% from "_content_notices.html" import content_notices %}'
        "<div data-report-notices>{{ content_notices(disclosures, corrections) }}</div>"
    )
    content = template.render(
        report={"public_id": "test-report"},
        disclosures=disclosures or [],
        corrections=corrections or [],
    )
    styles = (ROOT / "web/static/product-system.css").read_text()
    script = (ROOT / "web/static/content-notices.js").read_text()
    html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
      :root {{ --text: #f1f4f2; --muted: #aab6ad; --line: #263129;
               --accent: #b5ff62; --panel-2: #172019; --radius-xs: 4px; }}
      body {{ background: #080b09; margin: 16px; font-family: Arial, sans-serif; }}
      {styles}</style></head><body>{content}<script>{script}</script></body></html>"""
    page.route(
        "http://app.test/research/test-report",
        lambda route: route.fulfill(body=html, content_type="text/html"),
    )
    page.goto("http://app.test/research/test-report", wait_until="domcontentloaded")


@pytest.mark.parametrize("width", [390, 1280])
def test_public_notices_show_reason_and_time_and_escape_text(page: Page, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    text = '<img src=x onerror="window.injected=1"> & corrected claim'
    _open(
        page,
        disclosures=[_notice()],
        corrections=[
            _notice(
                id=2,
                kind="correction",
                label="Correction",
                text=text,
                reason="The source amended its filing.",
                recorded_by="operator",
            )
        ],
    )
    expect(page.locator(".content-notice-label")).to_have_text(
        ["Correction", "Disclosure · Holdings"]
    )
    expect(page.locator(".content-notice").first.locator("p").first).to_have_text(text)
    expect(page.locator(".content-notice-reason")).to_have_text(
        "Reason: The source amended its filing."
    )
    expect(page.locator(".content-notice-time").first).to_have_text(
        "2026-09-05 18:00 UTC · operator"
    )
    expect(page.locator(".content-notices img")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_editorial_notices_have_no_player_composer(page: Page) -> None:
    _open(page)
    expect(page.locator("textarea, input, select, form")).to_have_count(0)
