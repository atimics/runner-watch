"""Market routes search original identities and label selected sports teams."""

from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from bs4 import BeautifulSoup
from starlette.requests import Request

from runner_web import main, memecoins, stories
from runner_web.market_screens import detail, listing

ADDRESS = "So11111111111111111111111111111111111111112"


@pytest.fixture
def board_context(monkeypatch):
    monkeypatch.setattr(main, "enforce_rate", lambda *args, **kwargs: None)
    monkeypatch.setattr(stories, "stories_by_subject", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        main,
        "page_context",
        lambda request, session, **context: {
            "request": request,
            "user": None,
            "runners_origin": "https://runners.test",
            "sports_origin": "https://sports.test",
            "static_version": "test",
            **context,
        },
    )


def request(query):
    result = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": urlencode({"q": query}).encode(),
            "headers": [],
        }
    )
    result.state.csp_nonce = "test"
    return result


@pytest.mark.parametrize("query", ["dogwifhat", "WIF", ADDRESS, ADDRESS.lower()])
def test_memecoin_route_keeps_matches_through_both_filters(board_context, monkeypatch, query):
    now = datetime.now(UTC).isoformat()
    coin = {
        "id": "wif-token",
        "symbol": "So1111",
        "name": ADDRESS,
        "claimed_symbol": "WIF",
        "claimed_name": "dogwifhat",
        "token_address": ADDRESS,
        "price": 1.23,
        "change_24h": 2,
        "volume_24h": 100,
        "market_cap": 1000,
        "observed_at": now,
    }
    monkeypatch.setattr(
        memecoins,
        "_market_states",
        lambda: {
            "memecoins_snapshot": {"rows": [coin], "collected_at": now},
        },
    )
    monkeypatch.setattr(memecoins, "memecoins_enabled", lambda: True)
    response = main.memecoins_board_response(request(query), None, "list", query)
    html = BeautifulSoup(response.body, "html.parser")
    rows = html.select("a.ticker")
    assert len(rows) == 1
    assert rows[0]["href"] == "/memecoins/coin/wif-token"
    assert rows[0].select_one(".ticker-name strong").get_text() == "So1111…111112"
    assert rows[0].select_one(".ticker-name small").get_text() == "Creator-set: WIF"
    assert rows[0].select_one(".ticker-name small")["title"] == ADDRESS


def game(selection="home", **changes):
    result = {
        "id": "nba:example",
        "name": "Opening night",
        "league": "nba",
        "home_team_name": "Boston Celtics",
        "home_abbreviation": "BOS",
        "away_team_name": "Los Angeles Lakers",
        "away_abbreviation": "LAL",
        "status": "pre",
        "start_time": "2099-01-01T12:00:00Z",
        "prediction": {
            "selection": selection,
            "signal": "lean",
            "home_probability": 0.63,
            "away_probability": 0.37,
        },
    }
    result.update(changes)
    return result


@pytest.mark.parametrize("query", ["Boston Celtics", "Los Angeles Lakers", "Opening night"])
def test_sports_route_searches_full_names(board_context, monkeypatch, query):
    monkeypatch.setattr(main, "_public_screen_data", lambda *args: {"events": [game()]})
    monkeypatch.setattr(main, "_public_golf_data", lambda: {"events": []})
    response = main.sports_board_response(request(query), None, "list")
    html = BeautifulSoup(response.body, "html.parser")
    assert len(html.select("a.ticker")) == 1
    assert [team.get_text() for team in html.select(".sports-team")] == ["LAL", "BOS"]


@pytest.mark.parametrize("side", ["home", "away"])
def test_sports_forecast_and_tag_name_the_same_outcome(board_context, side):
    response = main._simple_board(request(""), None, "sports", [game(side)], "list")
    html = BeautifulSoup(response.body, "html.parser")
    assert html.select_one(".prediction-glyph [role=img]")["aria-label"].startswith(
        "BOS: sentiment pending a fresh model and comparable market price."
    )
    assert html.select_one(".tag").get_text() == "MODEL"
    assert html.select_one(".tag")["title"] == ("BOS: saved model; comparable fresh prices pending")


def test_selected_team_falls_back_to_full_name(board_context):
    response = main._simple_board(request(""), None, "sports", [game(home_abbreviation="")], "list")
    html = BeautifulSoup(response.body, "html.parser")
    assert html.select_one(".prediction-glyph [role=img]")["aria-label"].startswith(
        "Boston Celtics: sentiment pending a fresh model and comparable market price."
    )
    assert [team.get_text() for team in html.select(".sports-team")] == ["LAL", "Boston Celtics"]


def test_stock_search_keeps_its_existing_fields():
    stock = {"ticker": "BRR", "company": "Silvia Inc", "id": "hidden-id", "token_address": ADDRESS}
    assert len(listing("stocks", [stock], query="Silvia")["rows"]) == 1
    assert len(listing("stocks", [stock], query="brr")["rows"]) == 1
    assert listing("stocks", [stock], query="hidden-id")["rows"] == []
    assert listing("stocks", [stock], query=ADDRESS)["rows"] == []


def test_search_fields_stay_out_of_public_models():
    coin = {
        "id": "wif",
        "symbol": "WIF",
        "claimed_name": "dogwifhat",
        "token_address": "PRIVATE-SENTINEL",
    }
    listed = listing("memecoins", [coin], query="dogwifhat")
    opened = detail("memecoins", {"coin": coin})
    assert len(listed["rows"]) == 1
    for item in [listed["rows"][0], opened["item"]]:
        assert "search_text" not in item
        assert "PRIVATE-SENTINEL" not in str(item)
