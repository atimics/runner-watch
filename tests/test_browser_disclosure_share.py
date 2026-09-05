from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Page, Route, expect

from runner_web import main as web_main
from runner_web.content_notices import record_content_notice
from runner_web.db import connection
from tests.test_content_notices import notice_db  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.usefixtures("notice_db")]


@pytest.mark.parametrize("width,corrected", [(390, False), (1280, True)])
def test_first_disclosure_updates_real_report_metadata_and_share_without_reload(
    page: Page, monkeypatch, width: int, corrected: bool
) -> None:
    page.set_viewport_size({"width": width, "height": 844})
    page.set_default_timeout(5000)
    monkeypatch.setattr(web_main, "_ticker_summary", lambda _: None)
    with connection() as database:
        database.execute(
            "UPDATE research_commissions SET completed_at=created_at,usage_json=? WHERE id='stock'",
            (json.dumps({"context": {}}),),
        )
    if corrected:
        record_content_notice(
            "report",
            "public-stock",
            kind="correction",
            text="Revenue was $2.1 million.",
            reason="The source changed its units.",
        )
    client = TestClient(web_main.app, base_url=web_main.APP_ORIGIN)
    client.cookies.set(web_main.SESSION_COOKIE, "alice")
    saved = []

    def serve(route: Route) -> None:
        request = route.request
        response = client.request(
            request.method, request.url, headers=request.headers, content=request.post_data_buffer
        )
        if request.url.endswith("/disclosures"):
            saved.append(response.json())
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in {"content-encoding", "content-length"}
        }
        route.fulfill(status=response.status_code, headers=headers, body=response.content)

    page.route("**/*", serve)
    page.add_init_script(
        "Object.defineProperty(navigator, 'share', {value: async payload => "
        "{window.sharedReport = payload;}})"
    )
    try:
        page.goto(f"{web_main.APP_ORIGIN}/research/public-stock", wait_until="networkidle")
        page.get_by_role("button", name="Close announcement", exact=True).click()
        expect(page.locator('meta[name="description"]')).to_have_count(1)
        initial = page.locator('meta[property="og:title"]').get_attribute("content")
        assert "Disclosure" not in initial
        page.evaluate("window.sameReportPage = 'original-page'")
        page.get_by_text("Add a disclosure", exact=True).click()
        page.get_by_label("Relationship", exact=True).select_option("sponsorship")
        page.get_by_label("Disclosure", exact=True).fill("The issuer paid me for this report.")
        page.get_by_role("button", name="Save disclosure").click()
        expect(page.locator("[data-disclosure-status]")).to_have_text("Disclosure saved.")
        assert len(saved) == 1
        expected_title = (
            "FIX · Correction · Disclosure" if corrected else "FIX · Disclosure · Original headline"
        )
        expected_summary = (
            "Correction · Disclosure: Revenue was $2.1 million."
            if corrected
            else "Disclosure: Original summary"
        )
        assert saved[0]["share_title"] == expected_title
        assert saved[0]["share_summary"] == expected_summary
        for selector in ['meta[property="og:title"]', 'meta[name="twitter:title"]']:
            expect(page.locator(selector)).to_have_attribute("content", expected_title)
        for selector in [
            'meta[name="description"]',
            'meta[property="og:description"]',
            'meta[name="twitter:description"]',
        ]:
            expect(page.locator(selector)).to_have_attribute("content", expected_summary)
        page.get_by_role("button", name="Share this report", exact=True).click()
        assert page.evaluate("window.sharedReport") == {
            "title": expected_title,
            "text": expected_summary,
            "url": f"{web_main.APP_ORIGIN}/research/public-stock",
        }
        assert page.evaluate("window.sameReportPage") == "original-page"
        expect(page.locator("[data-disclosure-status]")).to_have_text("Disclosure saved.")
        expect(page.locator("h1")).to_have_text("Original headline")
    finally:
        client.close()
