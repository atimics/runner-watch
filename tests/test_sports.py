from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.main import home, origin_for_request, product_for_request, sports_game_page
from runner_web.sports import (
    create_sports_pick,
    implied_probability,
    no_vig_probabilities,
    normalize_event,
    predict_event,
    settle_picks,
    sports_event,
    sports_slate,
    store_events,
)


def request(
    host: str = "sports.rati.chat",
    path: str = "/",
    *,
    forwarded_host: str | None = None,
) -> Request:
    headers = [(b"host", host.encode())]
    if forwarded_host:
        headers.append((b"x-forwarded-host", forwarded_host.encode()))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": headers,
            "scheme": "https",
            "server": (host, 443),
            "client": ("127.0.0.1", 1234),
            "query_string": b"",
        }
    )


def sample_event(*, completed: bool = False) -> dict[str, object]:
    state = "post" if completed else "pre"
    return {
        "id": "401000001",
        "name": "Away Club at Home Club",
        "season": {"year": 2026, "type": 2, "slug": "regular-season"},
        "date": (datetime.now(UTC) + timedelta(hours=4)).isoformat(),
        "status": {
            "type": {
                "state": state,
                "completed": completed,
                "shortDetail": "Final" if completed else "7:00 PM",
            }
        },
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "score": "5" if completed else "0",
                        "team": {
                            "id": "1",
                            "displayName": "Home Club",
                            "abbreviation": "HOM",
                        },
                        "records": [{"name": "overall", "summary": "60-40"}],
                    },
                    {
                        "homeAway": "away",
                        "score": "3" if completed else "0",
                        "team": {
                            "id": "2",
                            "displayName": "Away Club",
                            "abbreviation": "AWY",
                        },
                        "records": [{"name": "overall", "summary": "48-52"}],
                    },
                ],
                "venue": {"fullName": "Receipt Park", "address": {"city": "Test City"}},
                "odds": [
                    {
                        "provider": {"name": "Example Book"},
                        "moneyline": {
                            "home": {"close": {"odds": "-130"}, "open": {"odds": "-125"}},
                            "away": {"close": {"odds": "+115"}, "open": {"odds": "+110"}},
                        },
                        "spread": -1.5,
                        "overUnder": 8.5,
                    }
                ],
            }
        ],
    }


@pytest.fixture
def sports_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "sports.db")
    init_db()


def test_moneyline_probabilities_remove_the_vig() -> None:
    assert implied_probability(150) == pytest.approx(0.4)
    assert implied_probability(-150) == pytest.approx(0.6)
    home, away = no_vig_probabilities(-110, -110)
    assert home == pytest.approx(0.5)
    assert away == pytest.approx(0.5)


def test_scoreboard_event_becomes_a_source_bound_prediction() -> None:
    event = normalize_event("mlb", sample_event())
    assert event is not None
    assert event["id"] == "mlb:401000001"
    assert event["home_odds"] == -130
    prediction = predict_event(event)
    assert prediction["model_version"] == "team-form-v1"
    assert prediction["home_probability"] > prediction["away_probability"]
    assert prediction["evidence"]
    assert prediction["risks"]


def test_preseason_game_is_never_promoted_as_an_edge() -> None:
    raw = sample_event()
    raw["season"] = {"year": 2026, "type": 1, "slug": "preseason"}
    event = normalize_event("nfl", raw)
    assert event is not None
    prediction = predict_event(event)
    assert prediction["selection"] == "pass"
    assert prediction["signal"] == "pass"
    assert prediction["edge"] is None


def test_paper_pick_freezes_odds_and_settles(sports_db) -> None:
    event = normalize_event("mlb", sample_event())
    assert event is not None
    store_events([event])
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) "
            "VALUES('user-1','member_1','Member','active',?)",
            (datetime.now(UTC).isoformat(),),
        )
    pick = create_sports_pick("user-1", event["id"], "home")
    assert pick["american_odds"] == -130
    assert pick["status"] == "open"
    assert "-" in pick["caller_handle"]

    final_event = normalize_event("mlb", sample_event(completed=True))
    assert final_event is not None
    store_events([final_event])
    assert settle_picks() == 1
    with connection() as database:
        settled = database.execute(
            "SELECT * FROM sports_picks WHERE id=?", (pick["id"],)
        ).fetchone()
    assert settled["result"] == "win"
    assert settled["return_units"] == pytest.approx(100 / 130, abs=0.0001)


def test_sports_host_gets_the_sports_product(sports_db) -> None:
    event = normalize_event("mlb", sample_event())
    assert event is not None
    store_events([event])
    sports_request = request()
    assert product_for_request(sports_request) == "sports"
    response = home(sports_request, None)
    assert response.status_code == 200
    assert b"RATi Sports" in response.body
    assert b'class="game-matchup"' in response.body
    assert b'class="ticker-team"' in response.body
    assert b'class="game-edge' in response.body

    detail = sports_event(event["id"])
    assert detail is not None
    detail_response = sports_game_page(event["id"], request(path=f"/game/{event['id']}"), None)
    assert detail_response.status_code == 200
    assert b"Make a paper pick" in detail_response.body


def test_slate_builds_fixed_side_edge_history(sports_db) -> None:
    first_raw = sample_event()
    first = normalize_event("mlb", first_raw)
    assert first is not None
    store_events([first], observed_at=datetime(2026, 8, 26, 18, 0, tzinfo=UTC))

    second_raw = sample_event()
    moneyline = second_raw["competitions"][0]["odds"][0]["moneyline"]
    moneyline["home"]["close"]["odds"] = "-150"
    moneyline["away"]["close"]["odds"] = "+130"
    second = normalize_event("mlb", second_raw)
    assert second is not None
    store_events([second], observed_at=datetime(2026, 8, 26, 18, 10, tzinfo=UTC))

    event = next(item for item in sports_slate("mlb")["events"] if item["id"] == first["id"])
    history = event["edge_history"]

    assert history["side"] == "home"
    assert history["team"] == "HOM"
    assert [point["edge_pct"] for point in history["points"]] == [5.4, 2.2]
    assert history["change_pct"] == -3.2
    assert len(history["plot_points"].split()) == 2
    assert "fell 3.2 points" in history["label"]


def test_cloudflare_forwarded_host_selects_the_public_product() -> None:
    sports_request = request(
        host="runner-watch-ratimics.fly.dev",
        forwarded_host="sports.rati.chat",
    )
    assert product_for_request(sports_request) == "sports"
    assert origin_for_request(sports_request) == "https://sports.rati.chat"

    unknown_request = request(
        host="runner-watch-ratimics.fly.dev",
        forwarded_host="not-rati.example",
    )
    assert product_for_request(unknown_request) == "runners"
