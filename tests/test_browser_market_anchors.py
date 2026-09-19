"""Mobile market anchors keep long names, saved sources and live evidence readable."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
from playwright.sync_api import expect

from runner_web.market_screens import detail, listing

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser
ADDRESS = "D9FLtSbZrQ4QoBP7izmnAZAkLFsdQt77vgo4rEL4qxGq"


def fixtures(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


screens = fixtures("test_market_screens")
reports = fixtures("test_report_anchor")


def open_html(page, html, refresh=None):
    # Serve each real asset so both detail refresh and assessment listeners run.
    page.route(
        "https://app.test/static/**",
        lambda route: route.fulfill(
            path=str(ROOT / "web/static" / route.request.url.split("/static/", 1)[1].split("?")[0])
        ),
    )
    page.route(
        "https://app.test/api/**",
        lambda route: route.fulfill(json=refresh if refresh is not None else {"points": []}),
    )
    page.route(
        "https://app.test/", lambda route: route.fulfill(content_type="text/html", body=html)
    )
    page.goto("https://app.test/")


def long_sample(kind):
    market = "memecoins" if kind == "coin" else "sports"
    raw = screens.sample(market)
    if kind == "coin":
        raw.update(symbol=ADDRESS, name=ADDRESS, token_address=ADDRESS)
    elif kind == "golf":
        raw = dict(
            id="golf:123",
            name="Biltmore Championship Asheville International",
            status="in",
            leaderboard=[
                dict(
                    player_name="Christopher Alexander Championship Leader",
                    position=1,
                    score_display="-20",
                ),
                dict(
                    player_name="Benjamin Montgomery Championship Runner Up",
                    position=2,
                    score_display="-18",
                ),
            ],
        )
    else:
        raw.update(
            away_team_name="North Carolina Agricultural and Technical State Aggies",
            home_team_name="California State University Bakersfield Roadrunners",
            away_abbreviation="",
            home_abbreviation="",
        )
    return market, raw


def no_overlap(first, second):
    a, b = first.bounding_box(), second.bounding_box()
    assert a and b
    assert (
        a["x"] + a["width"] <= b["x"] + 1
        or b["x"] + b["width"] <= a["x"] + 1
        or a["y"] + a["height"] <= b["y"] + 1
        or b["y"] + b["height"] <= a["y"] + 1
    )


@pytest.mark.parametrize("width", [320, 390, 430])
@pytest.mark.parametrize("kind", ["coin", "golf", "team"])
def test_long_market_list_names_fit(page, width, kind):
    market, raw = long_sample(kind)
    screen = listing(market, [raw])
    page.set_viewport_size(dict(width=width, height=844))
    open_html(page, screens.render(screen))
    row = page.locator(".ticker")
    expect(row).to_have_count(1)
    expect(row.locator(".ticker-name strong")).to_have_text(screen["rows"][0]["name"])
    no_overlap(row.locator(".ticker-name"), row.locator(".ticker-value"))
    no_overlap(row.locator(".ticker-value strong"), row.locator(".ticker-value small"))
    if row.locator(".row-assessment").count():
        no_overlap(row.locator(".ticker-name"), row.locator(".row-assessment"))
        no_overlap(row.locator(".ticker-value"), row.locator(".row-assessment"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(row).to_have_attribute("href", screen["rows"][0]["href"])


@pytest.mark.parametrize("width", [320, 390, 430])
@pytest.mark.parametrize("kind", ["coin", "golf", "team"])
def test_long_market_details_fit(page, width, kind):
    market, raw = long_sample(kind)
    screen = detail(market, {"coin": raw, "history": []} if kind == "coin" else raw)
    page.set_viewport_size(dict(width=width, height=844))
    open_html(page, screens.render(screen), screen)
    expect(page.locator(".asset-heading h1")).to_have_text(screen["item"]["name"])
    expect(page.get_by_role("region", name="RATi assessment")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for team in page.locator(".teams > div").all():
        no_overlap(team.locator("h2"), team.locator("strong"))


@pytest.mark.parametrize("width", [320, 390, 430])
@pytest.mark.parametrize("subject_type", ["coin", "sports_game"])
def test_saved_report_expands_full_text_and_sources(page, width, subject_type):
    headline = "A saved view of the championship and its evidence " + ADDRESS
    html = reports.render_report(subject_type, ticker=ADDRESS, company=ADDRESS, headline=headline)
    page.set_viewport_size(dict(width=width, height=844))
    open_html(page, html)
    expect(page.get_by_role("heading", level=1)).to_have_text(headline)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".report-expand > summary").filter(has_text="Full report").click()
    expect(page.get_by_text("Complete thesis", exact=True)).to_be_visible()
    page.locator("#report-sources > summary").click()
    page.locator(".all-sources > summary").click()
    expect(page.get_by_role("link", name="Original source")).to_be_visible()
    expect(page.get_by_role("link", name="Original source")).to_have_attribute(
        "href", "https://example.com/proof"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_detail_api_refresh_updates_assessment_evidence(page):
    market, raw = long_sample("coin")
    screen = detail(market, {"coin": raw, "history": []})
    refreshed = copy.deepcopy(screen)
    refreshed["item"]["assessment"].update(
        label="Updated saved assessment",
        value=72,
        unit="",
        tag="WATCH",
        tag_tone="watch",
        reason="Updated evidence from the latest saved observation.",
        as_of="2026-09-19 14:00 UTC",
        drivers=[
            dict(
                label="Saved liquidity",
                value=12345,
                unit=" USD",
                source_url="https://example.com/new",
            )
        ],
        risks=["Liquidity remains concentrated."],
    )
    page.set_viewport_size(dict(width=390, height=844))
    open_html(page, screens.render(screen), refreshed)
    expect(page.locator("[data-assessment-label]")).to_have_text("Updated saved assessment")
    expect(page.locator("[data-assessment-value]")).to_have_text("72")
    expect(page.locator("[data-assessment-reason]")).to_have_text(
        refreshed["item"]["assessment"]["reason"]
    )
    expect(page.locator("[data-assessment-time]")).to_contain_text("2026-09-19 14:00 UTC")
    source = page.locator("[data-assessment-drivers] a")
    expect(source).to_have_attribute("href", "https://example.com/new")
    page.locator("[data-assessment-evidence] summary").click()
    expect(page.locator("[data-assessment-risks]")).to_have_text("Liquidity remains concentrated.")


def test_coin_contract_copy_uses_the_full_address(page):
    market, raw = long_sample('coin')
    screen = detail(market, {'coin': raw, 'history': []})
    page.add_init_script("""
        Object.defineProperty(navigator, 'clipboard', {value: {
            writeText: async value => document.body.dataset.copiedAddress = value
        }});
    """)
    open_html(page, screens.render(screen), refresh=screen)
    page.get_by_role('button', name='Copy CA', exact=True).click()
    expect(page.get_by_role('button', name='Copied', exact=True)).to_be_visible()
    expect(page.locator('body')).to_have_attribute('data-copied-address', ADDRESS)
    page.locator('.token-contract summary').click()
    expect(page.locator('.token-contract code')).to_have_text(ADDRESS)
