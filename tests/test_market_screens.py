from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from starlette.requests import Request

from runner_web import main as web
from runner_web.market_screens import detail, listing, series

SENTINEL = "operator-only-secret"


def sample(market):
    if market == "sports":
        return {
            "id": "nba:123",
            "away_abbreviation": "BOS",
            "home_abbreviation": "NYK",
            "away_team_name": "Boston Celtics",
            "home_team_name": "New York Knicks",
            "league": "nba",
            "status": "in",
            "away_score": 72,
            "home_score": 68,
            "start_time": "2026-09-12T18:00:00Z",
            "view_state": {
                "started": True,
                "score_available": True,
                "label": "Live",
                "pick_state": "closed",
            },
            "source_error": SENTINEL,
            "receipt": {"logs": SENTINEL},
        }
    if market == "memecoins":
        return {
            "id": "solana-test",
            "symbol": "BONK",
            "name": "Bonk",
            "price": 0.000018,
            "change_24h": 4.2,
            "observed_at": datetime.now(UTC).isoformat(),
            "source_url": SENTINEL,
            "discovery": {"slot": SENTINEL},
            "token_address": SENTINEL,
        }
    return {
        "ticker": "OPK",
        "company": "OPKO Health",
        "price": 1.56,
        "change_pct": 5.4,
        "quote_time": datetime.now(UTC).isoformat(),
        "source": SENTINEL,
        "directional_thesis": {"model": SENTINEL},
    }


def render(screen, user=None):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [(b"host", b"app.test")],
            "scheme": "http",
            "server": ("app.test", 80),
        }
    )
    request.state.csp_nonce = "test"
    return web.templates.TemplateResponse(
        request,
        "market_screen.html",
        {
            "request": request,
            "screen": screen,
            "user": user,
            "runners_origin": "http://app.test",
            "sports_origin": "http://sports.test",
            "static_version": "test",
        },
    ).body.decode()


@pytest.mark.parametrize("market", ["stocks", "memecoins", "sports"])
@pytest.mark.parametrize("view", ["list", "map"])
def test_shared_board_only_renders_business_fields(market, view):
    screen = listing(
        market,
        [sample(market)],
        view=view,
        graph={"subjects": [{"key": "OPK", "actor_ids": ["private-actor"]}]},
    )
    html = render(screen)
    assert SENTINEL not in html
    assert "private-actor" not in html
    assert 'aria-label="View"' in html
    assert ">List</a>" in html and ">Map</a>" in html
    assert "screenData" not in html
    assert "desktop-workspace" not in html
    assert screen["rows"][0]["href"] in html


@pytest.mark.parametrize("market", ["stocks", "memecoins", "sports"])
def test_detail_payload_excludes_internal_fields(market):
    raw = sample(market)
    data = (
        {"coin": raw, "can_call": False, "history": []}
        if market == "memecoins"
        else {"ticker": "OPK", "company": "OPKO Health", "current": raw}
        if market == "stocks"
        else raw
    )
    screen = detail(market, data)
    assert SENTINEL not in json.dumps(screen)
    assert SENTINEL not in render(screen)
    assert screen["kind"] == "detail"
    assert len(screen["facts"]) <= 2


def test_sparse_chart_retains_real_samples_only():
    assert series(
        [
            {"time": "2026-09-12T12:00:00Z", "close": 1.5, "source": SENTINEL},
            {"time": "2026-09-12T12:05:00Z", "close": float("nan")},
            {"close": 1.6},
        ]
    ) == [{"time": "2026-09-12T12:00:00Z", "value": 1.5}]


def test_coin_actions_follow_quote_availability():
    data = {"coin": sample("memecoins"), "can_call": False}
    assert detail("memecoins", data)["actions"] == []
    data["can_call"] = True
    active = {"public_id": "call-1", "return_pct": 3.2}
    action = detail("memecoins", data, active_call=active)["actions"][0]
    assert action["endpoint"] == "/api/memecoin-calls/call-1/close"


def test_sports_closed_game_has_score_and_existing_call():
    screen = detail("sports", sample("sports"), my_pick={"result": "win"})
    assert screen["actions"] == []
    assert screen["teams"][0]["score"] == "72"
    assert screen["note"] == "Your Call · Win"


def test_search_applies_to_map_and_list():
    for view in ["list", "map"]:
        assert len(listing("stocks", [sample("stocks")], query="opko", view=view)["rows"]) == 1
        assert listing("stocks", [sample("stocks")], query="missing", view=view)["rows"] == []


@pytest.fixture
def screen_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runner_web import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "screens.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    monkeypatch.setattr(web, "enforce_rate", lambda *a, **kw: None)
    monkeypatch.setattr(web, "current_user", lambda *a: None)
    monkeypatch.setattr(web, "_public_screen_data", lambda kind, key, build: build())
    client = TestClient(web.app)
    yield client
    client.close()


@pytest.mark.parametrize(
    "path", ["/memecoins", "/memecoins?view=map", "/memecoins/coin/solana-test"]
)
def test_coin_routes_preserve_paused_price_and_recover(screen_client, monkeypatch, path):
    coin = {**sample("memecoins"), "stale": True}
    monkeypatch.setattr(web, "memecoin_market", lambda **kw: {"rows": [coin]})
    monkeypatch.setattr(web, "market_actor_map", lambda *a: {})
    monkeypatch.setattr(
        web,
        "_memecoin_detail_payload",
        lambda *a: {"coin": coin, "calls": [], "can_call": not coin["stale"]},
    )
    for stale in (True, False, True):
        coin["stale"] = stale
        response = screen_client.get(path)
        assert response.status_code == 200
        assert ("Price paused" in response.text) == stale
        assert SENTINEL not in response.text
        quote = screen_client.get("/api/screens/memecoins/solana-test/quote").json()
        assert quote["freshness"] == ("paused" if stale else "current")
        assert quote["change"] == ("Price paused" if stale else "+4.2%")
        assert quote["tone"] == ("neutral" if stale else "up")
        assert quote["time"]
        assert SENTINEL not in json.dumps(quote)


@pytest.mark.parametrize("path", ["/?league=nba", "/?league=nba&view=map", "/game/nba:123"])
def test_sports_routes_follow_confirmed_scores(screen_client, monkeypatch, path):
    from runner_web.sports import _game_view_state

    event = sample("sports")
    monkeypatch.setattr(web, "product_for_request", lambda *a: "sports")
    monkeypatch.setattr(web, "sports_slate", lambda *a: {"events": [event]})
    monkeypatch.setattr(web, "sports_event", lambda *a: event)
    for status, now, away, home, expected in [
        ("pre", "2026-09-12T17:00:00+00:00", 0, 0, "Sep 12 · 18:00 UTC"),
        ("pre", "2026-09-12T19:00:00+00:00", 0, 0, "Score pending"),
        ("in", "2026-09-12T19:01:00+00:00", None, None, "Score pending"),
        ("in", "2026-09-12T19:02:00+00:00", 0, 0, "0 – 0"),
        ("in", "2026-09-12T19:03:00+00:00", 72, 68, "72 – 68"),
        ("post", "2026-09-12T21:00:00+00:00", 102, 98, "102 – 98"),
    ]:
        event.update(status=status, away_score=away, home_score=home)
        event["view_state"] = _game_view_state(event, datetime.fromisoformat(now))
        response = screen_client.get(path)
        assert response.status_code == 200
        assert expected in response.text
        assert SENTINEL not in response.text
        screen = detail("sports", event)
        if not event["view_state"]["score_available"]:
            assert "0 – 0" not in response.text
            assert all(team["score"] == "—" for team in screen["teams"])
        else:
            assert screen["teams"][0]["score"] == str(away)
