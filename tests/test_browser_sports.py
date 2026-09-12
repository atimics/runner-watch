from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect
from starlette.requests import Request

from runner_web import main as web_main

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser


def _request(path: str = "/") -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"host", b"sports.rati.chat")],
            "scheme": "https",
            "server": ("sports.rati.chat", 443),
            "client": ("127.0.0.1", 1234),
            "query_string": b"",
        }
    )
    request.state.csp_nonce = "browser-test"
    return request


def _event(event_id: str, away: str, home: str) -> dict[str, Any]:
    return {
        "id": event_id,
        "league": "mlb",
        "away_abbreviation": away,
        "home_abbreviation": home,
        "away_team_name": f"{away} Club",
        "home_team_name": f"{home} Club",
        "model_winner_side": "away",
        "model_winner_team_name": f"{away} Club",
        "model_winner_abbreviation": away,
        "model_winner_coin_tone": "0",
        "model_winner_opponent_team_name": f"{home} Club",
        "model_winner_opponent_abbreviation": home,
        "model_winner_probability_pct": 58.2,
        "model_winner_label": "PROJECTED",
        "model_winner_detail_label": "BASELINE WINNER",
        "model_winner_aria_action": "is projected to beat",
        "model_winner_projected_score_display": "5.1",
        "model_winner_opponent_projected_score_display": "4.2",
        "model_probability_pct": 58.2,
        "market_probability_pct": 55.6,
        "signal_coin_tone": "0",
        "signal_abbreviation": away,
        "signal_team_name": f"{away} Club",
        "bovada_divergence_material": False,
        "bovada_divergence_team": None,
        "bovada_divergence_pct": None,
        "start_time": "2026-08-27T19:00:00+00:00",
        "prediction": {"signal": "lean", "edge_pct": 2.6},
        "edge_history": {
            "label": "Edge history",
            "plot_points": "2,18 46,12 90,7",
            "dot_x": 90,
            "dot_y": 7,
            "points": [1, 2, 3],
        },
        "series_more": [],
        "series_more_count": 0,
    }


def _pulse(*events: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": list(events),
        "signal_count": len(events),
        "display_count": len(events),
        "scanned_count": len(events),
        "source_status": "success",
        "source_error": "",
        "updated_at": "2026-08-27T18:00:00+00:00",
        "model": "sports-baseline-v1",
        "view": "signals",
        "league": "all",
        "leagues": [{"key": "mlb", "name": "MLB"}],
        "model_record": {
            "games": 12,
            "sample": {"target": 100, "message": "Still an early sample."},
        },
    }


def _radar(*events: dict[str, Any]) -> dict[str, Any]:
    rows = []
    for event in events:
        rows.append(
            {
                **event,
                "radar_kind": event.get("radar_kind", "market"),
                "radar_label": event.get("radar_label", "PRICE"),
                "radar_detail": event.get("radar_detail", "Market moved toward the value side."),
                "radar_value": event.get("radar_value", 2.1),
                "away_score": event.get("away_score", 0),
                "home_score": event.get("home_score", 0),
                "status_detail": event.get("status_detail", "Pregame"),
            }
        )
    return {
        "events": rows,
        "change_count": len(rows),
        "display_count": len(rows),
        "tracked_count": len(rows),
        "updated_at": "2026-08-27T18:00:00+00:00",
        "league": "all",
        "leagues": [{"key": "mlb", "name": "MLB"}],
    }


def _inline_static_assets(html: str) -> str:
    html = html.replace("<head>", '<head><base href="http://app.test/">')

    def stylesheet(match: re.Match[str]) -> str:
        path = ROOT / "web/static" / match.group(1)
        return f"<style>{path.read_text()}</style>"

    html = re.sub(
        r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
        stylesheet,
        html,
    )

    scripts = {
        "market-screen.js": (ROOT / "web/static/market-screen.js").read_text(),
        "desktop-workspace.js": (ROOT / "web/static/desktop-workspace.js").read_text(),
        "live-list.js": (ROOT / "web/static/live-list.js").read_text(),
        "ticker-row.js": (ROOT / "web/static/ticker-row.js").read_text(),
        "sports-live.js": (ROOT / "web/static/sports-live.js").read_text(),
    }

    def script(match: re.Match[str]) -> str:
        filename = match.group(1)
        source = scripts.get(filename)
        if not source:
            return ""
        if filename == "desktop-workspace.js":
            return f'<script>addEventListener("DOMContentLoaded", () => {{ {source} }});</script>'
        return f"<script>{source}</script>"

    return re.sub(
        r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>',
        script,
        html,
    )


def _rendered_pulse(monkeypatch, payload: dict[str, Any]) -> str:
    monkeypatch.setattr(web_main, "sports_slate", lambda *_: payload)
    monkeypatch.setattr(web_main, "_public_screen_data", lambda area, key, build: build())
    monkeypatch.setattr(web_main, "_public_golf_data", lambda: {"events": []})
    monkeypatch.setattr(web_main, "enforce_rate", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        web_main,
        "page_context",
        lambda request, session, **values: {
            "request": request,
            "user": None,
            "runners_origin": "http://app.test",
            "sports_origin": "http://sports.test",
            "static_version": "test",
            **values,
        },
    )
    return _inline_static_assets(web_main.home(_request(), None).body.decode())


def _rendered_radar(monkeypatch, payload: dict[str, Any]) -> str:
    monkeypatch.setattr(
        web_main,
        "_public_sports_radar_data",
        lambda *_args, **_kwargs: {"radar": payload},
    )
    response = web_main.sports_radar_response(_request("/radar"), None)
    return _inline_static_assets(response.body.decode())


def _rendered_game_detail(latest_commission: dict[str, Any] | None = None) -> str:
    event = {
        "id": "closed-game",
        "league": "mlb",
        "start_time": "2026-08-29T18:20:00+00:00",
        "status": "pre",
        "status_detail": "11:20 AM",
        "completed": False,
        "venue": "Wrigley Field",
        "location": "Chicago, IL",
        "away_abbreviation": "CIN",
        "away_team_name": "Cincinnati Reds",
        "away_record": "68-66",
        "away_score": None,
        "home_abbreviation": "CHC",
        "home_team_name": "Chicago Cubs",
        "home_record": "76-58",
        "home_score": None,
        "source_url": "https://example.test/game",
        "paper_odds": None,
        "odds": {
            "away_odds": 186,
            "home_odds": -186,
            "sportsbook": "Market consensus",
            "source_label": "No-vig consensus via The Odds API",
            "observed_at": "2026-08-29T18:15:00+00:00",
        },
        "market_comparison": {"books": []},
        "prediction": {
            "selection": "away",
            "signal": "watch",
            "edge_pct": 6.4,
            "away_probability": 0.413,
            "home_probability": 0.587,
            "away_market_probability": 0.35,
            "home_market_probability": 0.65,
            "model_version": "sports-baseline-v1",
        },
        "receipt": None,
        "model_record": {
            "games": 24,
            "sample": {"label": "24 of 100 graded", "target": 100, "remaining": 76},
        },
        "model_winner_coin_tone": 0,
        "model_winner_abbreviation": "CHC",
        "model_winner_detail_label": "BASELINE WINNER",
        "model_winner_team_name": "Chicago Cubs",
        "model_winner_probability_pct": 58.7,
        "edge_history": {
            "label": "CIN model edge grew 1.4 points; now +6.4 percentage points",
            "points": [1, 2, 3, 4],
            "plot_points": "2,14 31,12 61,10 90,7",
            "dot_y": 7,
            "start_pct": 5.0,
            "current_pct": 6.4,
        },
        "context": {
            "headline": "Recent team form",
            "back_to_back": False,
            "series_game_count": 1,
            "previous_meeting": None,
            "head_to_head": {"meetings": 0},
            "recent_form": [],
        },
        "matchup_players": [],
        "news": [],
        "picks": [],
        "odds_history": [
            {
                "observed_at": "2026-08-29T18:15:00+00:00",
                "away_odds": 186,
                "home_odds": -186,
                "source_label": "No-vig consensus via The Odds API",
            }
        ],
        "view_state": {
            "label": "Game started",
            "detail": "Score pending from ESPN",
            "started": True,
            "score_available": False,
            "pick_state": "closed",
            "picks_open": False,
        },
    }
    response = web_main.templates.TemplateResponse(
        request=_request("/game/closed-game"),
        name="sports_game.html",
        context={
            "event": event,
            "latest_commission": latest_commission,
            "flash_report": {
                "href": None,
                "state": "closed",
                "label": "Reports closed",
                "detail": "Game has started",
                "enabled": False,
                "job_id": None,
                "message": "",
                "status_tone": None,
            },
            "sports_path_prefix": "",
            "call_win_flash_cap": 10,
            "my_pick": None,
            "pick_rewards": {"away": 0, "home": 0},
            "comments": [],
            "comment_count": 0,
            "active_tab": "pulse",
            "nav_product": "sports",
            "user": None,
            "static_version": "test",
            "runners_origin": "https://rati.chat",
            "sports_origin": "https://sports.rati.chat",
        },
    )
    return _inline_static_assets(response.body.decode())


def _rendered_open_game_with_slip(
    *, user: dict[str, Any] | None, my_pick: dict | None = None
) -> str:
    event = {
        "id": "open-game",
        "league": "mlb",
        "start_time": "2026-08-29T23:20:00+00:00",
        "status": "pre",
        "status_detail": "6:20 PM",
        "completed": False,
        "venue": "Wrigley Field",
        "location": "Chicago, IL",
        "away_abbreviation": "CIN",
        "away_team_name": "Cincinnati Reds",
        "away_record": "68-66",
        "away_score": None,
        "home_abbreviation": "CHC",
        "home_team_name": "Chicago Cubs",
        "home_record": "76-58",
        "home_score": None,
        "source_url": "https://example.test/game",
        "paper_odds": {
            "away_odds": 150,
            "home_odds": -170,
            "sportsbook": "Market consensus",
            "source_label": "No-vig consensus via The Odds API",
            "observed_at": "2026-08-29T18:15:00+00:00",
        },
        "odds": {
            "away_odds": 150,
            "home_odds": -170,
            "sportsbook": "Market consensus",
            "source_label": "No-vig consensus via The Odds API",
            "observed_at": "2026-08-29T18:15:00+00:00",
        },
        "market_comparison": {"books": []},
        "prediction": None,
        "receipt": None,
        "model_record": {
            "games": 24,
            "sample": {"label": "24 of 100 graded", "target": 100, "remaining": 76},
        },
        "model_winner_coin_tone": 0,
        "model_winner_abbreviation": "CHC",
        "model_winner_detail_label": "BASELINE WINNER",
        "model_winner_team_name": "Chicago Cubs",
        "model_winner_probability_pct": 58.7,
        "edge_history": None,
        "context": {
            "headline": "Recent team form",
            "back_to_back": False,
            "series_game_count": 1,
            "previous_meeting": None,
            "head_to_head": {"meetings": 0},
            "recent_form": [],
        },
        "matchup_players": [],
        "news": [],
        "picks": [],
        "odds_history": [],
        "view_state": {
            "label": "Pregame",
            "detail": "6:20 PM",
            "started": False,
            "score_available": False,
            "pick_state": "open",
            "picks_open": True,
        },
    }
    response = web_main.templates.TemplateResponse(
        request=_request("/game/open-game"),
        name="sports_game.html",
        context={
            "event": event,
            "latest_commission": None,
            "flash_report": {
                "href": None,
                "state": "closed",
                "label": "Reports closed",
                "detail": "Game has started",
                "enabled": False,
                "job_id": None,
                "message": "",
                "status_tone": None,
            },
            "sports_path_prefix": "",
            "call_win_flash_cap": 50,
            "my_pick": my_pick,
            "pick_rewards": {"away": 38, "home": 15},
            "comments": [],
            "comment_count": 0,
            "active_tab": "pulse",
            "nav_product": "sports",
            "user": user,
            "flash_wallet": {"balance": 120, "report_cost": 100},
            "caller_summary": {"handle": "swift-ibis"},
            "comment_avatar": web_main.comment_avatar_profile(
                "Quiet Signal", "browser-seed", "filing_sleuth"
            ),
            "static_version": "test",
            "runners_origin": "https://rati.chat",
            "sports_origin": "https://sports.rati.chat",
        },
    )
    return _inline_static_assets(response.body.decode())


def test_game_detail_shows_a_confirmation_slip_with_the_exact_payout() -> None:
    html = _rendered_open_game_with_slip(user={"id": "user-1"})

    assert 'data-pick="away" data-team="Cincinnati Reds" data-odds="+150" data-reward="38"' in html
    assert 'data-pick="home" data-team="Chicago Cubs" data-odds="-170" data-reward="15"' in html
    assert 'id="pickSlip" hidden' in html
    assert "Freeze Call" in html
    assert "Settles" in html
    assert "One Call per game, no undo." in html
    assert "about +38 Flash" not in html  # exact numbers only fill the slip after a tap


def test_game_detail_shows_your_call_ticket_instead_of_pick_buttons() -> None:
    html = _rendered_open_game_with_slip(
        user={"id": "user-1"},
        my_pick={
            "selection": "away",
            "american_odds": 150,
            "status": "open",
            "result": None,
            "reward_flash": 0,
        },
    )

    assert "Your Call" in html
    assert "CIN +150" in html
    assert "OPEN" in html
    assert '<button type="button" data-pick=' not in html  # pick buttons are gone
    assert "Settles when the game ends." in html


def test_game_detail_ticket_shows_a_won_call_with_its_flash_reward() -> None:
    html = _rendered_open_game_with_slip(
        user={"id": "user-1"},
        my_pick={
            "selection": "home",
            "american_odds": -170,
            "status": "settled",
            "result": "win",
            "reward_flash": 15,
        },
    )

    assert "Your Call" in html
    assert "CHC -170" in html
    assert "WON · +15 Flash" in html


def _load(
    page: Page,
    html: str,
    poll_payloads: list[dict[str, Any]],
    radar_payloads: list[dict[str, Any]] | None = None,
) -> list[BaseException]:
    errors: list[BaseException] = []
    page.on("pageerror", lambda error: errors.append(error))

    def pulse_response(route: Route) -> None:
        payload = poll_payloads.pop(0) if poll_payloads else _pulse()
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/pulse*", pulse_response)

    def radar_response(route: Route) -> None:
        payload = radar_payloads.pop(0) if radar_payloads else _radar()
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/radar*", radar_response)
    page.route(
        "**/game/**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><title>Game</title><main>Game detail</main>",
        ),
    )
    page.route(
        "http://app.test/",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    page.goto("http://app.test/", wait_until="domcontentloaded")
    return errors


def test_sports_list_refresh_preserves_search_and_page(page: Page, monkeypatch) -> None:
    page.clock.install()
    old = _event("old-game", "OLD", "HME")
    new = _event("new-game", "NEW", "NOW")
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(old)), [])
    html = _rendered_pulse(monkeypatch, _pulse(new, old))
    page.route("http://app.test/", lambda route: route.fulfill(content_type="text/html", body=html))
    page.get_by_role("searchbox").fill("a search in progress")
    page.clock.fast_forward(60000)
    expect(page.locator(".ticker")).to_have_count(2)
    expect(page.get_by_role("searchbox")).to_have_value("a search in progress")
    expect(page.get_by_role("searchbox")).to_be_focused()
    expect(page).to_have_url("http://app.test/")
    assert errors == []


def test_sports_list_empty_state_is_simple(page: Page, monkeypatch) -> None:
    payload = {**_pulse(), "updated_at": None, "source_error": "internal log"}
    errors = _load(page, _rendered_pulse(monkeypatch, payload), [])
    expect(page.get_by_role("heading", name="A quiet moment")).to_be_visible()
    expect(page.get_by_text("internal log")).to_have_count(0)
    assert errors == []


def test_sports_radar_applies_changes_without_reloading_or_losing_detail(
    page: Page, monkeypatch
) -> None:
    old = _event("radar-game", "AWY", "HME")
    changed = {**old, "radar_value": 4.4}
    page.set_viewport_size({"width": 1280, "height": 800})
    html = _rendered_radar(monkeypatch, _radar(old))
    errors = _load(page, html, [], [_radar(changed)])
    frame = page.locator("[data-desktop-frame]")
    original_detail = frame.get_attribute("src")

    page.evaluate("window.sportsRadarLive.poll()")

    refresh = page.locator("#sportsRadarRefresh")
    assert refresh.is_visible()
    assert refresh.text_content() == "Radar updated"
    assert frame.get_attribute("src") == original_detail
    assert "+2.1pp" in page.locator(".radar-value").text_content()

    refresh.click()

    assert "+4.4pp" in page.locator(".radar-value").text_content()
    assert frame.get_attribute("src") == original_detail
    assert page.locator(".radar-mark").first.evaluate(
        "node => ({"
        "radius: getComputedStyle(node).borderRadius, "
        "rightRule: getComputedStyle(node).borderRightWidth"
        "})"
    ) == {"radius": "0px", "rightRule": "1px"}
    assert errors == []


def test_sports_list_opens_a_single_detail_screen(page: Page, monkeypatch) -> None:
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(_event("game-1", "ONE", "TWO"))), [])
    page.locator(".ticker").click()
    expect(page).to_have_url("http://app.test/game/game-1")
    expect(page.get_by_text("Game detail")).to_be_visible()
    assert errors == []


def test_game_detail_prioritizes_actions_and_closes_started_actions(page: Page) -> None:
    html = _rendered_game_detail()
    page.set_viewport_size({"width": 1280, "height": 800})
    page.route(
        "http://app.test/",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    page.goto("http://app.test/", wait_until="domcontentloaded")

    app = page.locator(".sports-game-app")
    grid = page.locator(".game-detail-grid")
    assert app.evaluate("node => Math.round(node.getBoundingClientRect().width)") == 1120
    assert grid.evaluate("node => getComputedStyle(node).display") == "block"
    assert grid.evaluate("node => Math.round(node.getBoundingClientRect().width)") == 860
    assert page.locator(".probability-row").count() == 2
    assert page.locator(".decision-movement .edge-spark").bounding_box()["width"] > 200
    assert page.get_by_role("heading", name="Game thread").count() == 0
    assert page.get_by_role("heading", name="Calls closed").is_visible()
    assert page.get_by_text("Score pending from ESPN").is_visible()
    assert page.get_by_text("Log in to make a Call").count() == 0
    assert (
        page.locator(".game-disclosure > summary b").first.evaluate(
            "node => getComputedStyle(node).fontSize"
        )
        == "9px"
    )
    assert page.evaluate(
        """() => {
          const style = selector => getComputedStyle(document.querySelector(selector));
          return {
            stripRadius: style('.match-strip').borderRadius,
            boardRadius: style('.decision-board').borderRadius,
            modelCallRadius: style('.decision-model-call').borderRadius,
            valueCallRule: style('.decision-value-call').borderLeftWidth,
            metricRadius: style('.value-numbers > span').borderRadius,
            notebookRadius: style('.game-notebook').borderRadius,
            notebookItemRadius: style('.game-notebook .game-disclosure').borderRadius,
            actionRadius: style('.game-actions > .paper-pick').borderRadius,
            flashRadius: style('.game-actions > .game-flash').borderRadius,
          };
        }"""
    ) == {
        "stripRadius": "0px",
        "boardRadius": "0px",
        "modelCallRadius": "0px",
        "valueCallRule": "1px",
        "metricRadius": "0px",
        "notebookRadius": "0px",
        "notebookItemRadius": "0px",
        "actionRadius": "0px",
        "flashRadius": "0px",
    }
    assert (
        page.locator(".game-actions").bounding_box()["y"]
        < page.locator(".game-notebook").bounding_box()["y"]
    )

    page.set_viewport_size({"width": 390, "height": 800})
    assert grid.evaluate("node => getComputedStyle(node).display") == "block"
    assert app.evaluate("node => Math.round(node.getBoundingClientRect().width)") == 390
    assert page.locator(".decision-value-call").evaluate(
        "node => ({"
        "left: getComputedStyle(node).borderLeftWidth, "
        "top: getComputedStyle(node).borderTopWidth"
        "})"
    ) == {"left": "0px", "top": "1px"}
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.mark.parametrize("width", [390, 900, 1280])
def test_sports_list_respects_shared_responsive_breakpoints(
    page: Page, monkeypatch, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(_event("game-1", "AWY", "HME"))), [])
    expect(page.get_by_role("navigation", name="Market").get_by_role("link")).to_have_count(3)
    expect(page.get_by_role("navigation", name="View").get_by_role("link")).to_have_count(2)
    expect(page.locator(".ticker")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert errors == []


def test_sports_list_shows_real_scores_in_the_shared_ticker_row(page: Page, monkeypatch) -> None:
    event = {
        **_event("game-1", "AWY", "HME"),
        "status": "in",
        "away_score": 5,
        "home_score": 2,
    }
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(event)), [])
    expect(page.locator(".ticker-name strong")).to_have_text("AWY · HME")
    expect(page.locator(".ticker-value strong")).to_have_text("5 – 2")
    expect(page.locator(".ticker-value small")).to_have_text("In progress")
    assert errors == []


def test_sports_list_keeps_operator_predictions_private(page: Page, monkeypatch) -> None:
    event = {**_event("game-1", "AWY", "HME"), "model_winner_label": "internal model detail"}
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(event)), [])
    expect(page.get_by_text("internal model detail")).to_have_count(0)
    expect(page.locator(".ticker-value strong")).to_have_text("vs")
    assert errors == []


def test_sports_refresh_keeps_keyboard_focus_on_a_ticker(page: Page, monkeypatch) -> None:
    page.clock.install()
    event = _event("game-1", "AWY", "HME")
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(event)), [])
    page.locator(".ticker").focus()
    empty = _rendered_pulse(monkeypatch, _pulse())
    page.route(
        "http://app.test/", lambda route: route.fulfill(content_type="text/html", body=empty)
    )
    page.clock.fast_forward(60000)
    expect(page.locator(".ticker")).to_be_focused()
    page.get_by_role("searchbox").focus()
    page.clock.fast_forward(60000)
    expect(page.get_by_role("heading", name="A quiet moment")).to_be_visible()
    assert errors == []


def test_sports_alpha_opens_its_leader_in_the_shared_detail_pane(page: Page, monkeypatch) -> None:
    board = {
        "rows": [
            {
                "href": "/game/alpha-game",
                "coin_tone": "0",
                "coin_label": "AWY",
                "ticker": "AWY",
                "company": "Away Club",
                "price_label": "+120",
                "change_tone": "up",
                "change_label": "+4",
                "active_calls": 3,
                "total_calls": 5,
                "odds_label": "+120",
                "pulse_label": "Lean",
                "rank": 1,
            }
        ],
        "calls": [],
        "contenders": [],
        "active_calls": 3,
        "total_calls": 5,
        "league": "all",
        "leagues": [{"key": "mlb", "name": "MLB"}],
    }
    monkeypatch.setattr(web_main, "_sports_alpha_data", lambda *_args, **_kwargs: board)
    page.set_viewport_size({"width": 1280, "height": 800})
    response = web_main.sports_alpha_response(_request("/alpha"), None)
    html = _inline_static_assets(response.body.decode())
    errors = _load(page, html, [])

    assert page.locator("[data-desktop-frame]").get_attribute("src") == "/game/alpha-game"
    assert (
        page.locator('a[href="/game/alpha-game"][data-desktop-default]')
        .get_attribute("class")
        .endswith("desktop-panel-selected")
    )
    assert errors == []


def test_sports_forecast_keeps_model_context_next_to_its_estimate(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.set_content(
        _rendered_game_detail(
            {
                "locked": False,
                "actor": {"ladder_position": 1, "ladder_size": 2, "model_label": "test/model"},
                "sports_forecast": {
                    "selection": "away",
                    "selected_abbreviation": "SEA",
                    "selected_probability": 0.57,
                    "selected_team": "Seattle",
                    "agrees_with_baseline": True,
                },
            }
        ),
        wait_until="domcontentloaded",
    )
    forecast = page.locator(".game-flash-prediction")
    assert "SEA 57%" in forecast.inner_text()
    assert "test/model" in forecast.inner_text()
    assert forecast.locator(".game-forecast-context").inner_text() == (
        "AI estimate from the report’s saved inputs. Actual outcomes can differ."
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
