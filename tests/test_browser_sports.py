from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route
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
        "desktop-workspace.js": (ROOT / "web/static/desktop-workspace.js").read_text(),
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
    pick_stats = {
        "settled": 12,
        "wins": 7,
        "losses": 5,
        "pushes": 0,
        "units": 1.4,
        "roi_pct": 11.7,
    }
    monkeypatch.setattr(
        web_main,
        "_public_sports_pulse_data",
        lambda *_args, **_kwargs: {"pulse": payload, "pick_stats": pick_stats},
    )
    monkeypatch.setattr(
        web_main,
        "_public_golf_data",
        lambda: {
            "events": [],
            "display_count": 0,
            "entrant_count": 0,
            "source_status": "success",
            "source_error": "",
            "updated_at": None,
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


def _rendered_game_detail() -> str:
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
            "sports_call_reward_cap": 10,
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

    page.route("**/api/sports/pulse*", pulse_response)

    def radar_response(route: Route) -> None:
        payload = radar_payloads.pop(0) if radar_payloads else _radar()
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/sports/radar*", radar_response)
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


def test_sports_pulse_applies_updates_without_reloading_or_losing_detail(
    page: Page, monkeypatch
) -> None:
    old = _event("old-game", "OLD", "HME")
    new = _event("new-game", "NEW", "NOW")
    page.set_viewport_size({"width": 1280, "height": 800})
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(old)), [_pulse(new, old)])
    frame = page.locator("[data-desktop-frame]")
    original_detail = frame.get_attribute("src")

    page.evaluate("window.sportsPulseLive.poll()")

    refresh = page.locator("#sportsPulseRefresh")
    assert refresh.is_visible()
    assert refresh.text_content() == "1 new matchup"
    assert frame.get_attribute("src") == original_detail
    assert page.locator('a[href="/game/new-game"]').count() == 0

    refresh.click()

    assert page.locator('a[href="/game/new-game"]').count() == 1
    assert frame.get_attribute("src") == original_detail
    assert page.locator('a[href="/game/old-game"]').get_attribute("class").endswith(
        "desktop-panel-selected"
    )
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
    assert errors == []


def test_sports_detail_panel_stays_dark_while_a_game_loads(
    page: Page, monkeypatch
) -> None:
    first = _event("first-game", "ONE", "HME")
    second = _event("second-game", "TWO", "HME")
    page.set_viewport_size({"width": 1280, "height": 800})
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(first, second)), [])
    frame = page.locator("[data-desktop-frame]")
    loading = page.locator("[data-desktop-loading]")

    frame.wait_for(state="visible")
    assert loading.is_hidden()

    loading_state = page.evaluate(
        """() => {
          document.querySelector('a[href="/game/second-game"]').click();
          const frame = document.querySelector('[data-desktop-frame]');
          const loading = document.querySelector('[data-desktop-loading]');
          return {
            frameHidden: frame.hidden,
            loadingHidden: loading.hidden,
            loadingBackground: getComputedStyle(loading).backgroundColor,
          };
        }"""
    )

    assert loading_state == {
        "frameHidden": True,
        "loadingHidden": False,
        "loadingBackground": "rgb(9, 12, 10)",
    }
    frame.wait_for(state="visible")
    assert frame.get_attribute("src") == "/game/second-game"
    assert loading.is_hidden()
    assert errors == []


def test_game_detail_prioritizes_thread_and_closes_started_actions(page: Page) -> None:
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
    assert page.get_by_role("heading", name="Game thread").is_visible()
    assert page.get_by_role("heading", name="Paper picks closed").is_visible()
    assert page.get_by_text("Score pending from ESPN").is_visible()
    assert page.get_by_text("Log in to make a paper pick").count() == 0
    assert page.locator(".game-disclosure > summary b").first.evaluate(
        "node => getComputedStyle(node).fontSize"
    ) == "9px"
    assert page.locator("#discussion").bounding_box()["y"] < page.locator(
        ".game-notebook"
    ).bounding_box()["y"]

    page.set_viewport_size({"width": 390, "height": 800})
    assert grid.evaluate("node => getComputedStyle(node).display") == "block"
    assert app.evaluate("node => Math.round(node.getBoundingClientRect().width)") == 390


@pytest.mark.parametrize("width", [390, 900, 1280])
def test_sports_pulse_respects_shared_responsive_breakpoints(
    page: Page, monkeypatch, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(_event("game-1", "AWY", "HME"))), [])

    assert page.locator(".screen-head").is_visible()
    assert page.locator(".tab-bar").is_visible()
    assert page.locator(".product-tab-link").is_visible()
    navigation = page.locator(".tab-bar")
    navigation_box = navigation.bounding_box()
    assert navigation_box is not None
    assert page.evaluate(
        "getComputedStyle(document.querySelector('.tab-bar')).gridTemplateColumns.split(' ').length"
    ) == 4
    links = navigation.locator(".tab-link")
    assert links.count() == 4
    for index in range(links.count()):
        link_box = links.nth(index).bounding_box()
        assert link_box is not None
        assert link_box["y"] >= navigation_box["y"]
        assert link_box["y"] + link_box["height"] <= navigation_box["y"] + navigation_box["height"]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    if width < 900:
        assert page.locator(".desktop-detail-panel").is_hidden()
    else:
        assert page.locator(".desktop-detail-panel").is_visible()
        assert page.locator("[data-desktop-frame]").get_attribute("src") == "/game/game-1"
    assert errors == []


def test_sports_pulse_uses_the_exact_ticker_row_contract(
    page: Page, monkeypatch
) -> None:
    event = {
        **_event("split-decision", "AWY", "HME"),
        "signal_abbreviation": "HME",
        "signal_team_name": "HME Club",
        "model_probability_pct": 41.8,
        "market_probability_pct": 34.0,
        "prediction": {"signal": "watch", "edge_pct": 7.8},
    }
    page.set_viewport_size({"width": 390, "height": 800})
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(event)), [])

    row = page.locator('[data-sports-pulse-row="split-decision"]')
    copy = row.text_content()
    assert "PROJECTED" in copy
    assert "58%" in copy
    assert "Value HME +7.8pp" in copy
    assert "MODEL41.8%MARKET34.0%" in copy
    assert "vs HME · MLB" in copy
    assert row.get_attribute("class") == "token-row ticker-row sports-pulse-row"
    assert row.locator(".coin").count() == 1
    assert row.locator(".ticker-line strong").text_content() == "AWY"
    assert row.locator(".company-name").text_content() == "AWY Club"
    assert row.locator(".mini-chart.loaded").count() == 1
    assert page.locator(".winner-card").count() == 0
    assert page.locator(".winner-quote").count() == 0
    assert page.locator(".model-favorite").count() == 0
    assert row.get_attribute("aria-label") == (
        "AWY Club is projected to beat HME Club with a 58 percent win chance; "
        "value side HME with a +7.8 percentage-point model edge, model 41.8 percent "
        "versus market 34.0 percent"
    )
    assert page.evaluate(
        """() => {
          const row = document.querySelector('[data-sports-pulse-row="split-decision"]');
          const coin = row.querySelector('.coin');
          const chart = row.querySelector('.mini-chart');
          const quote = row.querySelector('.quote');
          const style = getComputedStyle(row);
          return {
            display: style.display,
            minHeight: style.minHeight,
            padding: style.padding,
            radius: style.borderRadius,
            coinWidth: getComputedStyle(coin).width,
            chartWidth: getComputedStyle(chart).width,
            chartHeight: getComputedStyle(chart).height,
            quoteAlign: getComputedStyle(quote).textAlign,
          };
        }"""
    ) == {
        "display": "grid",
        "minHeight": "94px",
        "padding": "9px 8px",
        "radius": "0px",
        "coinWidth": "42px",
        "chartWidth": "64px",
        "chartHeight": "18px",
        "quoteAlign": "right",
    }
    assert errors == []


def test_sports_pulse_calls_a_close_projection_a_slight_edge(
    page: Page, monkeypatch
) -> None:
    event = {
        **_event("close-game", "AWY", "HME"),
        "model_winner_probability_pct": 51.2,
        "model_winner_label": "SLIGHT EDGE",
        "model_winner_detail_label": "BASELINE LEAN",
        "model_winner_aria_action": "has a slight model edge over",
    }
    page.set_viewport_size({"width": 390, "height": 800})
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse(event)), [])

    row = page.locator('[data-sports-pulse-row="close-game"]')
    assert "SLIGHT EDGE" in row.text_content()
    assert "51%" in row.text_content()
    assert "Value AWY +2.6pp" in row.text_content()
    assert row.get_attribute("aria-label") == (
        "AWY Club has a slight model edge over HME Club with a 51 percent win chance; "
        "value side AWY with a +2.6 percentage-point model edge, model 58.2 percent "
        "versus market 55.6 percent"
    )
    assert errors == []


def test_nba_pulse_renders_a_clear_between_seasons_state(page: Page, monkeypatch) -> None:
    pulse = {
        **_pulse(),
        "league": "nba",
        "scanned_count": 12,
        "empty_state": {
            "kind": "season-break",
            "title": "NBA is between seasons.",
            "detail": (
                "The next scheduled game is MIA at TOR. Pulse will wait for regular-season "
                "records and fresh market consensus before publishing a projection."
            ),
            "next_start_time": "2026-10-03T23:00:00+00:00",
            "status_label": "Next NBA game scheduled",
        },
    }
    page.set_viewport_size({"width": 390, "height": 800})
    errors = _load(page, _rendered_pulse(monkeypatch, _pulse()), [pulse])

    page.evaluate("window.sportsPulseLive.poll()")
    refresh = page.locator("#sportsPulseRefresh")
    assert refresh.is_visible()
    refresh.click()

    empty = page.locator(".sports-season-break")
    assert empty.is_visible()
    assert "NBA is between seasons." in empty.text_content()
    assert "MIA at TOR" in empty.text_content()
    assert "Next tipoff" in empty.text_content()
    assert page.locator("#sportsPulseMaturity").text_content() == "Next NBA game scheduled"
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
    assert page.locator('a[href="/game/alpha-game"][data-desktop-default]').get_attribute(
        "class"
    ).endswith("desktop-panel-selected")
    assert errors == []


def test_sports_game_thread_posts_and_removes_a_comment(page: Page) -> None:
    posted: list[dict[str, str]] = []

    def create_comment(route: Route) -> None:
        posted.append(route.request.post_data_json)
        route.fulfill(
            status=201,
            content_type="application/json",
            body=json.dumps(
                {
                    "comment": {
                        "id": "comment-1",
                        "body": posted[-1]["body"],
                        "created_at": "2026-08-30T18:00:00+00:00",
                        "alias": "Signal Moth",
                        "is_owner": True,
                        "avatar": {
                            "name": "Signal Moth",
                            "ability": "Form Reader",
                            "ability_description": "Checks recent form.",
                            "tone": 1,
                            "frame": 2,
                            "eyes": 3,
                            "signal": 4,
                        },
                    },
                    "count": 1,
                }
            ),
        )

    page.route("**/api/sports/games/**/comments", create_comment)
    page.route(
        "**/api/sports/comments/comment-1",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"deleted": True, "id": "comment-1"}),
        ),
    )
    script = (ROOT / "web/static/sports-comments.js").read_text()
    html = f"""
    <!doctype html><html><body>
      <section data-sports-comments data-event-id="mlb:game-1">
        <b id="discussionCount">0</b>
        <form id="sportsCommentForm"><textarea id="sportsCommentBody"></textarea>
          <small id="sportsCommentStatus"></small><button type="submit">Post comment</button>
        </form>
        <ol id="commentList"></ol><p id="commentEmpty">No comments.</p>
      </section>
      <script>{script}</script>
    </body></html>
    """
    page.route(
        "http://comments.test/",
        lambda route: route.fulfill(status=200, content_type="text/html", body=html),
    )
    page.goto("http://comments.test/", wait_until="domcontentloaded")

    page.locator("#sportsCommentBody").fill("Bullpen depth decides this one.")
    page.get_by_role("button", name="Post comment").click()

    page.locator("#commentList li").wait_for()
    assert posted == [{"body": "Bullpen depth decides this one."}]
    assert page.locator("#discussionCount").text_content() == "1"
    assert page.locator("#commentList li").text_content().endswith(
        "Bullpen depth decides this one."
    )
    assert page.locator("#commentEmpty").is_hidden()

    page.get_by_role("button", name="delete").click()

    page.locator("#commentList li").wait_for(state="detached")
    assert page.locator("#commentList li").count() == 0
    assert page.locator("#discussionCount").text_content() == "0"
    assert page.locator("#commentEmpty").is_visible()
