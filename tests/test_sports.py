from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.main import (
    alpha_page,
    home,
    origin_for_request,
    product_for_request,
    radar_page,
    sports_game_page,
)
from runner_web.sports import (
    create_sports_pick,
    implied_probability,
    no_vig_probabilities,
    normalize_event,
    normalize_news_articles,
    normalize_player_appearances,
    predict_event,
    settle_picks,
    sports_alpha,
    sports_event,
    sports_pulse,
    sports_radar,
    sports_slate,
    store_events,
    store_news_articles,
    store_player_appearances,
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


def test_recent_team_news_is_attached_only_to_promoted_matchups(sports_db) -> None:
    event = normalize_event("mlb", sample_event())
    assert event is not None
    collected_at = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
    payload = {
        "articles": [
            {
                "id": "team-news-1",
                "headline": "Home Club starter returns before the series opener",
                "description": "The starter was activated Wednesday.",
                "published": "2026-08-26T17:30:00Z",
                "source": "AP",
                "links": {"web": {"href": "https://example.test/home-club-news"}},
                "categories": [{"type": "team", "team": {"id": "1"}}],
            },
            {
                "id": "unrelated-news",
                "headline": "Another club makes a move",
                "published": "2026-08-26T17:00:00Z",
                "links": {"web": {"href": "https://example.test/other-club-news"}},
                "categories": [{"type": "team", "team": {"id": "99"}}],
            },
        ]
    }

    articles = normalize_news_articles([event], payload, collected_at)

    assert len(articles) == 1
    assert articles[0]["event_id"] == event["id"]
    assert articles[0]["team_side"] == "home"
    assert articles[0]["source_name"] == "AP"
    store_events([event], observed_at=collected_at)
    assert store_news_articles(articles) == 1
    detail = sports_event(str(event["id"]))
    assert detail is not None
    assert detail["news_count"] == 1
    assert detail["news"][0]["headline"].startswith("Home Club starter")

    pass_raw = sample_event()
    pass_raw["season"] = {"year": 2026, "type": 1, "slug": "preseason"}
    pass_event = normalize_event("mlb", pass_raw)
    assert pass_event is not None
    assert normalize_news_articles([pass_event], payload, collected_at) == []


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
    assert b'id="sportsAnnouncement"' in response.body
    assert b'data-announcement-version="sports-intro-2026-08-26"' in response.body
    assert b"localStorage.getItem(announcementKey)" in response.body
    assert b"Today\xe2\x80\x99s slate" in response.body

    detail = sports_event(event["id"])
    assert detail is not None
    detail_response = sports_game_page(event["id"], request(path=f"/game/{event['id']}"), None)
    assert detail_response.status_code == 200
    assert b'class="decision-team"' in detail_response.body
    assert b"Team news" in detail_response.body
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


def test_sports_pulse_hides_passes_and_radar_keeps_real_moves(sports_db) -> None:
    promoted_raw = sample_event()
    promoted = normalize_event("mlb", promoted_raw)
    assert promoted is not None
    store_events([promoted], observed_at=datetime(2026, 8, 26, 18, 0, tzinfo=UTC))

    moved_raw = sample_event()
    moneyline = moved_raw["competitions"][0]["odds"][0]["moneyline"]
    moneyline["home"]["close"]["odds"] = "-105"
    moneyline["away"]["close"]["odds"] = "-105"
    moved = normalize_event("mlb", moved_raw)
    assert moved is not None
    store_events([moved], observed_at=datetime(2026, 8, 26, 18, 10, tzinfo=UTC))

    pass_raw = sample_event()
    pass_raw["id"] = "401000002"
    pass_raw["season"] = {"year": 2026, "type": 1, "slug": "preseason"}
    passed = normalize_event("nfl", pass_raw)
    assert passed is not None
    store_events([passed])

    pulse = sports_pulse()
    assert [event["id"] for event in pulse["events"]] == [promoted["id"]]
    assert pulse["hidden_count"] == 1

    radar = sports_radar()
    assert radar["events"][0]["id"] == promoted["id"]
    assert radar["events"][0]["radar_kind"] == "market"


def test_sports_alpha_builds_team_and_player_win_rate_history(sports_db) -> None:
    payload = {
        "boxscore": {
            "players": [
                {
                    "team": {"id": "1", "displayName": "Home Club", "abbreviation": "HOM"},
                    "statistics": [
                        {
                            "labels": ["PTS"],
                            "athletes": [
                                {
                                    "athlete": {"id": "p1", "displayName": "Home Player"},
                                    "position": {"abbreviation": "P"},
                                    "starter": True,
                                    "stats": ["20"],
                                }
                            ],
                        }
                    ],
                },
                {
                    "team": {"id": "2", "displayName": "Away Club", "abbreviation": "AWY"},
                    "statistics": [
                        {
                            "labels": ["PTS"],
                            "athletes": [
                                {
                                    "athlete": {"id": "p2", "displayName": "Away Player"},
                                    "position": {"abbreviation": "P"},
                                    "starter": True,
                                    "stats": ["10"],
                                }
                            ],
                        }
                    ],
                },
            ]
        }
    }
    for event_number in range(1, 4):
        raw = sample_event(completed=True)
        raw["id"] = f"40100000{event_number}"
        event = normalize_event("mlb", raw)
        assert event is not None
        store_events([event])
        store_player_appearances(normalize_player_appearances(event, payload))

    alpha = sports_alpha("mlb")
    home_team = next(team for team in alpha["teams"] if team["abbreviation"] == "HOM")
    player = next(row for row in alpha["players"] if row["player_id"] == "p1")
    assert home_team["win_rate"] == 60.0
    assert home_team["history"]
    assert player["win_rate"] == 100.0
    assert player["games"] == 3


def test_sports_host_has_radar_and_alpha_products(sports_db) -> None:
    radar_response = radar_page(request(path="/radar"), None)
    alpha_response = alpha_page(request(path="/alpha"), None)
    assert radar_response.status_code == 200
    assert b"Only material changes" in radar_response.body
    assert alpha_response.status_code == 200
    assert b"player win rate" in alpha_response.body


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
