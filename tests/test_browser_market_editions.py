"""Daily editions expose the watch, sources, and exact chart readings on phones."""

import importlib.util
from pathlib import Path

import pytest
from jinja2 import ChainableUndefined, Environment, FileSystemLoader
from playwright.sync_api import expect

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser
spec = importlib.util.spec_from_file_location(
    "edition_samples", ROOT / "scripts/preview_market_editions.py"
)
samples = importlib.util.module_from_spec(spec)
spec.loader.exec_module(samples)


def open_report(page, post):
    report = samples.sample_report(post)
    env = Environment(
        loader=FileSystemLoader(ROOT / "web/templates"),
        autoescape=True,
        undefined=ChainableUndefined,
    )
    html = env.get_template("market_report_detail.html").render(
        report=report,
        app_origin="https://app.test",
        runners_origin="https://app.test",
        sports_origin="https://sports.test",
        static_version="test",
        user=None,
        request={"state": {"csp_nonce": "test"}, "url": {"path": report["permalink"]}},
    )
    page.route(
        "https://app.test/static/**",
        lambda route: route.fulfill(
            path=str(ROOT / "web/static" / route.request.url.split("/static/", 1)[1].split("?")[0])
        ),
    )
    page.route(
        "https://app.test/", lambda route: route.fulfill(content_type="text/html", body=html)
    )
    page.goto("https://app.test/")


@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("post", [False, True])
def test_daily_editions_keep_the_watch_and_sources_readable(page, width, post):
    page.set_viewport_size({"width": width, "height": 844})
    open_report(page, post)
    expect(page.get_by_role("heading", level=1)).to_have_count(1)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".edition-quick-watch").bounding_box()["y"] < 760
    page.get_by_role("link", name="Watch board & targets ↓").click()
    expect(page.locator("#watch-DEMO")).to_be_in_viewport()
    if post:
        page.get_by_role("link", name="Company profile ↓").click()
        expect(page.locator(".report-risk-summary")).to_be_visible()
        assert (
            page.locator(".report-company-facts a").first.evaluate(
                "(el) => parseFloat(getComputedStyle(el).fontSize)"
            )
            >= 12
        )
        page.locator(".report-price-chart summary").press("Enter")
        expect(page.locator(".report-price-chart table")).to_be_visible()
        assert page.locator(".report-price-chart tbody tr").count() == 7
        page.locator(".report-selection summary").press("Enter")
        expect(page.locator(".report-interest")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
