"""Cup forecasts, pairings, and rosters stay usable at each screen size."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect
from starlette.requests import Request

from runner_web import main as web
from runner_web.golf_cup import build_analysis
from runner_web.market_screens import detail
from runner_web.sports import golf_market_context

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser


def render_cup(*, status="pre", points=("0", "0"), external_id="401824815", teams=True):
    golf = {
        "id": f"golf:{external_id}",
        "external_id": external_id,
        "name": "Presidents Cup" if external_id == "401824815" else "Team Championship",
        "scoring_format": "match_play",
        "status": status,
        "display_status": {"pre": "Scheduled", "in": "In progress", "post": "Final"}[status],
        "completed": status == "post",
        "start_time": "2026-09-24T16:35:00Z",
        "last_collected_at": "2026-09-24T02:58:00Z",
        "venue": "",
        "location": "",
        "source_url": f"https://www.espn.com/golf/leaderboard/_/tournamentId/{external_id}",
        "leaderboard": [],
        "teams": [
            {"name": name, "abbreviation": code, "points": float(score), "points_display": score}
            for name, code, score in [
                ("USA", "USA", points[0]),
                ("International", "INTL", points[1]),
            ]
        ]
        if teams
        else [],
    }
    if external_id == "401824815" and teams:
        sources = json.loads((ROOT / "tests/fixtures/golf_cup_2026.json").read_text())
        sources["matches"]["data"]["points"] = {"1": float(points[0]), "3": float(points[1])}
        sources["matches"]["data"]["state"] = status
        if status == "post":
            for match in sources["matches"]["data"]["matches"]:
                match.update(completed=True, state="post")
        golf["analysis"] = build_analysis(sources)
    request = Request({"type": "http", "path": "/", "headers": [], "query_string": b""})
    request.state.csp_nonce = "test"
    return web.templates.env.get_template("sports_golf_detail.html").render(
        request=request,
        screen=detail("sports", golf),
        golf=golf,
        golf_context=golf_market_context(golf),
        user=None,
        runners_origin="https://app.test",
        sports_origin="https://app.test",
        static_version="test",
    )


def open_cup(page, **options):
    page.route(
        "https://app.test/static/**",
        lambda route: route.fulfill(
            path=str(ROOT / "web/static" / route.request.url.split("/static/", 1)[1].split("?")[0])
        ),
    )
    page.route("https://app.test/api/**", lambda route: route.fulfill(json={}))
    page.route(
        "https://app.test/",
        lambda route: route.fulfill(content_type="text/html", body=render_cup(**options)),
    )
    page.goto("https://app.test/")


@pytest.mark.parametrize("width", [320, 390, 768, 1280])
def test_cup_forecast_pairings_rosters_and_keyboard_method_fit(page, width):
    page.set_viewport_size({"width": width, "height": 900})
    open_cup(page)
    expect(page.get_by_role("heading", level=1)).to_have_text("Presidents Cup")
    expect(page.get_by_role("heading", name="USA", exact=True)).to_have_count(2)
    expect(page.locator(".cup-win-chance")).to_contain_text("81")
    expect(page.locator(".cup-pairing")).to_have_count(5)
    expect(page.locator(".cup-roster-grid tbody tr")).to_have_count(24)
    expect(page.locator(".cup-pairing").first).to_contain_text("Scottie Scheffler / Sam Burns")
    expect(page.locator(".cup-pairing").first).to_contain_text("Sungjae Im / Min Woo Lee")
    expect(page.locator(".cup-probability-labels")).to_contain_text("Tied Cup")
    method = page.locator(".cup-method").first.locator("summary")
    method.press("Enter")
    expect(
        page.get_by_text("The model assumes a 12% tie chance per match.", exact=False)
    ).to_be_visible()
    method.press("Enter")
    expect(page.locator(".cup-method").first).not_to_have_attribute("open", "")
    page.locator(".cup-analysis-sources summary").click()
    expect(page.get_by_role("link", name="ESPN pairings and match scores")).to_have_attribute(
        "href", "https://www.espn.com/golf/leaderboard?tournamentId=401824815"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for side in page.locator(".cup-pairing-sides > div").all():
        assert side.evaluate("el => el.scrollWidth <= el.clientWidth")


@pytest.mark.parametrize("status, points", [("in", ("6.5", "3.5")), ("post", ("18.5", "11.5"))])
def test_cup_retains_saved_scores_during_and_after_play(page, status, points):
    page.set_viewport_size({"width": 320, "height": 900})
    open_cup(page, status=status, points=points)
    expect(page.locator(".cup-current-score strong")).to_have_text(list(points))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if status == "post":
        expect(page.locator(".cup-prediction")).to_contain_text("Result confirmed")
        expect(page.locator(".cup-prediction")).to_contain_text("18.5")


def test_other_match_play_events_have_their_own_context(page):
    open_cup(page, external_id="123")
    expect(page.get_by_role("heading", level=1)).to_have_text("Team Championship")
    expect(page.locator(".cup-prediction")).to_have_count(0)
    expect(page.get_by_role("meter")).to_have_count(0)


def test_cup_pending_sources_explains_the_forecast_inputs(page):
    open_cup(page, teams=False)
    expect(page.get_by_role("heading", name="Preparing the Cup forecast")).to_be_visible()
    expect(page.locator(".cup-win-chance")).to_have_count(0)


def test_saved_scores_refresh_and_keep_the_method_open(page):
    page.clock.install()
    open_cup(page)
    page.locator(".cup-method").first.locator("summary").click()
    page.locator("a.back").focus()
    page.route(
        "https://app.test/",
        lambda route: route.fulfill(
            content_type="text/html", body=render_cup(status="in", points=("6.5", "3.5"))
        ),
    )
    page.clock.fast_forward(61_000)
    expect(page.locator(".cup-current-score strong")).to_have_text(["6.5", "3.5"])
    expect(page.locator(".cup-current-score")).to_contain_text("In progress")
    expect(page.locator(".cup-method").first).to_have_attribute("open", "")
