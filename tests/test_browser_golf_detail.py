"""Cup scores and official sources stay usable at each screen size."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import expect
from starlette.requests import Request

from runner_web import main as web
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
def test_cup_scores_sources_and_keyboard_guide_fit(page, width):
    page.set_viewport_size({"width": width, "height": 900})
    open_cup(page, status="in", points=("6.5", "3.5"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Presidents Cup")
    expect(page.locator(".cup-points strong")).to_have_text(["6.5", "3.5"])
    expect(page.locator(".cup-status")).to_have_text("In progress")
    expect(page.get_by_role("meter", name="USA progress to 15.5 points")).to_have_attribute(
        "value", "6.5"
    )
    expect(page.locator(".cup-session-list li")).to_have_count(5)
    expect(page.get_by_role("link", name="Official matches and scoring")).to_have_attribute(
        "href", "https://www.presidentscup.com/scoring"
    )
    expect(page.get_by_role("link", name="2026 team rosters")).to_have_attribute(
        "href", "https://www.presidentscup.com/teams"
    )
    expect(page.locator(".golf-detail-source")).to_contain_text("2026-09-24 02:58 UTC")
    guide = page.locator(".cup-formats summary")
    guide.press("Enter")
    expect(page.get_by_text("Partners take turns playing one ball.", exact=False)).to_be_visible()
    guide.press("Enter")
    expect(page.locator(".cup-formats")).not_to_have_attribute("open", "")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for team in page.locator(".cup-team").all():
        assert team.evaluate("el => el.scrollWidth <= el.clientWidth")


@pytest.mark.parametrize("status, points", [("pre", ("0", "0")), ("post", ("18.5", "11.5"))])
def test_cup_retains_saved_scores_before_and_after_play(page, status, points):
    page.set_viewport_size({"width": 320, "height": 900})
    open_cup(page, status=status, points=points)
    expect(page.locator(".cup-points strong")).to_have_text(list(points))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for team in page.locator(".cup-team").all():
        assert team.evaluate("el => el.scrollWidth <= el.clientWidth")
    if status == "post":
        expect(page.get_by_role("meter", name="USA progress to 15.5 points")).to_have_attribute(
            "value", "15.5"
        )


def test_other_match_play_events_have_their_own_context(page):
    open_cup(page, external_id="123")
    expect(page.get_by_role("heading", level=1)).to_have_text("Team Championship")
    expect(page.locator(".cup-session-list")).to_have_count(0)
    expect(page.get_by_role("link", name="Official matches and scoring")).to_have_count(0)
    expect(page.get_by_role("meter")).to_have_count(0)


def test_cup_pending_teams_keeps_the_official_score_link(page):
    open_cup(page, teams=False)
    expect(page.get_by_role("heading", name="Teams pending")).to_be_visible()
    expect(page.get_by_role("link", name="Official matches and scoring")).to_be_visible()
