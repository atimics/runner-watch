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
        "model_winner_probability_pct": 58.2,
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
        "sports-live.js": (ROOT / "web/static/sports-live.js").read_text(),
    }

    def script(match: re.Match[str]) -> str:
        source = scripts.get(match.group(1))
        return f"<script>{source}</script>" if source else ""

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
    return _inline_static_assets(web_main.home(_request(), None).body.decode())


def _rendered_radar(monkeypatch, payload: dict[str, Any]) -> str:
    monkeypatch.setattr(
        web_main,
        "_public_sports_radar_data",
        lambda *_args, **_kwargs: {"radar": payload},
    )
    response = web_main.sports_radar_response(_request("/radar"), None)
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


def test_sports_pulse_separates_value_side_from_baseline_winner(
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

    card = page.locator(".winner-card")
    copy = card.text_content()
    assert "VALUE SIDE" in copy
    assert "Model 42% · Market 34% · Edge +7.8 percentage points" in copy
    assert "Baseline winner · AWY 58%" in copy
    assert page.locator(".model-favorite").count() == 0
    assert card.get_attribute("aria-label") == (
        "AWY Club at HME Club; baseline winner AWY at 58 percent; "
        "value side HME with a +7.8 percentage-point model edge"
    )
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
