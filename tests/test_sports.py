from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Request

from runner_web import db
from runner_web import main as web_main
from runner_web import sports as sports_module
from runner_web.db import connection, init_db
from runner_web.main import (
    alpha_page,
    home,
    origin_for_request,
    product_for_request,
    radar_page,
    sports_game_page,
    sports_receipts_page,
)
from runner_web.sports import (
    collect_stored_player_appearances,
    create_sports_pick,
    fetch_league_history_chunk,
    implied_probability,
    no_vig_probabilities,
    normalize_event,
    normalize_news_articles,
    normalize_player_appearances,
    predict_event,
    settle_picks,
    sports_alpha,
    sports_alpha_board,
    sports_event,
    sports_flash_evidence,
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


def test_event_linked_recap_stays_with_previous_game_and_builds_series_context(
    sports_db,
) -> None:
    collected_at = datetime(2026, 8, 27, 2, 30, tzinfo=UTC)
    previous_raw = sample_event(completed=True)
    previous_raw["id"] = "401816681"
    previous_raw["date"] = "2026-08-26T22:45:00Z"
    previous_raw["competitions"][0]["competitors"][0]["score"] = "1"
    previous_raw["competitions"][0]["competitors"][1]["score"] = "13"
    previous = normalize_event("mlb", previous_raw)
    assert previous is not None
    previous["home_odds"] = None
    previous["away_odds"] = None

    upcoming_raw = sample_event()
    upcoming_raw["id"] = "401816695"
    upcoming_raw["date"] = "2026-08-27T17:05:00Z"
    upcoming = normalize_event("mlb", upcoming_raw)
    assert upcoming is not None

    payload = {
        "articles": [
            {
                "id": "49737429",
                "headline": "Away Club rolls past Home Club in the series opener",
                "published": "2026-08-27T02:01:31Z",
                "source": "AP",
                "links": {"web": {"href": "https://example.test/recap?gameId=401816681"}},
                "categories": [
                    {"type": "team", "team": {"id": "1"}},
                    {"type": "team", "team": {"id": "2"}},
                    {"type": "event", "event": {"id": "401816681"}},
                ],
            }
        ]
    }

    articles = normalize_news_articles([previous, upcoming], payload, collected_at)

    assert [article["event_id"] for article in articles] == [previous["id"]]
    store_events([previous, upcoming], observed_at=collected_at)
    stale = {**articles[0], "id": "stale-link", "event_id": upcoming["id"]}
    store_news_articles([stale])
    store_news_articles(
        articles,
        replace_event_ids=[str(previous["id"]), str(upcoming["id"])],
    )

    detail = sports_event(str(upcoming["id"]))
    assert detail is not None
    assert detail["news"] == []
    assert detail["context"]["back_to_back"] is True
    assert detail["context"]["series_game_number"] == 2
    assert detail["context"]["series_game_count"] == 2
    assert detail["context"]["previous_meeting"]["id"] == previous["id"]
    assert detail["context"]["previous_meeting"]["recap"]["external_id"] == "49737429"
    assert detail["context"]["head_to_head"]["away_wins"] == 1
    assert detail["context"]["recent_form"][0]["record"] == "1-0"
    assert detail["context"]["recent_form"][1]["record"] == "0-1"
    assert detail["context"]["recent_form"][0]["short_rest"] is True
    assert "since last start" in detail["context"]["recent_form"][0]["rest_label"]

    response = sports_game_page(
        str(upcoming["id"]),
        request(path=f"/game/{upcoming['id']}"),
        None,
    )
    assert b"Back-to-back rematch" in response.body
    assert b"GAME RECAP" in response.body
    assert b"Game 2 of 2" in response.body
    assert b"timeZoneName:'short'" in response.body


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

    board = sports_alpha_board("mlb")
    assert board["active_calls"] == 1
    assert board["rows"][0]["ticker"] == "HOM"
    assert board["rows"][0]["price_label"].endswith("¢")
    assert board["calls"][0]["entry_label"] == "-130"

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

    settled_board = sports_alpha_board("mlb")
    assert settled_board["active_calls"] == 0
    assert settled_board["calls"][0]["status"] == "win"
    assert settled_board["calls"][0]["return_label"].endswith("u")


def test_sports_host_gets_the_sports_product(sports_db) -> None:
    event = normalize_event("mlb", sample_event())
    assert event is not None
    store_events([event])
    sports_request = request()
    assert product_for_request(sports_request) == "sports"
    response = home(sports_request, None)
    assert response.status_code == 200
    assert b"RATi Sports" in response.body
    assert b'class="game-card winner-card"' in response.body
    assert b'class="winner-coin ' in response.body
    assert b"Away Club" in response.body
    assert b"Home Club" in response.body
    assert b"MARKET-TOTAL SCORE" in response.body
    assert b"MODEL" in response.body
    assert b"EDGE" in response.body
    assert b"distinct matchups" in response.body
    assert b'class="edge-spark"' in response.body
    assert b"PROJECTED WINNER" not in response.body
    assert b"Full slate" not in response.body
    assert b'class="sports-hero"' not in response.body
    assert b'class="sports-scoreboard pulse-summary"' not in response.body
    assert b'id="sportsAnnouncement"' not in response.body
    assert b"localStorage.getItem(announcementKey)" not in response.body

    detail = sports_event(event["id"])
    assert detail is not None
    detail_response = sports_game_page(event["id"], request(path=f"/game/{event['id']}"), None)
    assert detail_response.status_code == 200
    assert b'class="decision-team"' in detail_response.body
    assert b"PROJECTED WINNER" in detail_response.body
    assert b"VALUE READ" in detail_response.body
    assert b"They can point to different teams" in detail_response.body
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


def test_slate_database_query_count_does_not_grow_per_event(
    sports_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = []
    for number in range(12):
        raw = sample_event()
        raw["id"] = f"401100{number:03d}"
        event = normalize_event("mlb", raw)
        assert event is not None
        events.append(event)
    store_events(events)

    statements: list[str] = []

    class CountingDatabase:
        def __init__(self, database: Any) -> None:
            self.database = database

        def execute(self, statement: str, parameters: Any = ()):
            statements.append(" ".join(statement.split()))
            return self.database.execute(statement, parameters)

    @contextmanager
    def counted_connection():
        with connection() as database:
            yield CountingDatabase(database)

    monkeypatch.setattr(sports_module, "connection", counted_connection)

    assert len(sports_slate("mlb")["events"]) >= len(events)
    assert len(statements) == 6


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
    assert pulse["events"][0]["signal_team_name"] == "Home Club"
    assert pulse["events"][0]["signal_abbreviation"] == "HOM"
    assert pulse["events"][0]["signal_coin_tone"] in range(5)
    assert pulse["events"][0]["model_winner_abbreviation"] == "HOM"
    assert pulse["events"][0]["projected_home_score"] > pulse["events"][0]["projected_away_score"]

    full_slate_request = sports_pulse(view="all")
    assert full_slate_request["view"] == "signals"
    assert [event["id"] for event in full_slate_request["events"]] == [promoted["id"]]

    radar = sports_radar()
    assert radar["events"][0]["id"] == promoted["id"]
    assert radar["events"][0]["radar_kind"] == "market"


def test_pulse_separates_model_favorite_from_value_edge(sports_db) -> None:
    raw = sample_event()
    competitors = raw["competitions"][0]["competitors"]
    competitors[0]["records"][0]["summary"] = "60-73"
    competitors[1]["records"][0]["summary"] = "82-51"
    moneyline = raw["competitions"][0]["odds"][0]["moneyline"]
    moneyline["home"]["close"]["odds"] = "+180"
    moneyline["away"]["close"]["odds"] = "-215"
    event = normalize_event("mlb", raw)
    assert event is not None
    store_events([event])

    card = sports_pulse()["events"][0]

    assert card["signal_abbreviation"] == "HOM"
    assert card["model_winner_abbreviation"] == "AWY"
    assert card["model_winner_probability_pct"] > 50
    assert card["projected_away_score"] > card["projected_home_score"]
    assert card["projected_score_basis"] == "market total"


def test_pulse_groups_repeated_series_after_the_strongest_game(sports_db) -> None:
    first = normalize_event("mlb", sample_event())
    assert first is not None
    second_raw = sample_event()
    second_raw["id"] = "401000002"
    second_raw["date"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    second = normalize_event("mlb", second_raw)
    assert second is not None
    store_events([first, second])

    pulse = sports_pulse("mlb")

    assert pulse["signal_count"] == 2
    assert pulse["display_count"] == 1
    assert len(pulse["events"]) == 1
    assert pulse["events"][0]["series_more_count"] == 1
    assert pulse["events"][0]["series_more"][0]["id"] == second["id"]


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
    assert home_team["history_points"][0]["at"]
    assert player["win_rate"] == 100.0
    assert player["games"] == 3
    assert player["history_points"][-1]["rate"] == 100.0
    assert alpha["model"]["games"] == 3
    assert alpha["model"]["history_points"]
    assert alpha["model"]["sample"]["label"] == "VERY EARLY SAMPLE"
    assert alpha["model"]["sample"]["target"] == 250
    assert len(alpha["model"]["receipts"]) == 3
    assert alpha["model"]["receipts"][0]["receipt_id"]


def test_game_page_keeps_player_context_and_flash_inside_the_matchup(sports_db) -> None:
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
    for number, days_ago in enumerate((2, 1), start=1):
        raw = sample_event(completed=True)
        raw["id"] = f"40120000{number}"
        raw["date"] = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
        past_event = normalize_event("mlb", raw)
        assert past_event is not None
        store_events([past_event])
        store_player_appearances(normalize_player_appearances(past_event, payload))

    upcoming_raw = sample_event()
    upcoming_raw["id"] = "401200003"
    upcoming = normalize_event("mlb", upcoming_raw)
    assert upcoming is not None
    store_events([upcoming])

    detail = sports_event(str(upcoming["id"]))
    assert detail is not None
    home = next(team for team in detail["matchup_players"] if team["side"] == "home")
    assert home["players"][0]["name"] == "Home Player"
    assert home["players"][0]["games"] == 2
    assert home["players"][0]["wins"] == 2
    assert home["players"][0]["last_stats_label"] == "PTS 20"

    fingerprint, evidence = sports_flash_evidence(str(upcoming["id"]))
    assert len(fingerprint) == 64
    assert evidence["subject_type"] == "sports_game"
    assert evidence["event_id"] == upcoming["id"]
    assert evidence["players"][0]["players"]
    assert all("caller_handle" not in item for item in evidence["public_picks"].values())

    response = sports_game_page(
        str(upcoming["id"]),
        request(path=f"/game/{upcoming['id']}"),
        None,
    )
    assert b"Daily Flash" in response.body
    assert b"Players relevant to this game" in response.body
    assert b"Home Player" in response.body
    assert b"no global player list" in response.body
    assert b"/api/sports/games/mlb:401200003/research" in response.body


def test_sports_flash_uses_the_shared_daily_report_lifecycle(
    sports_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = normalize_event("mlb", sample_event())
    assert event is not None
    store_events([event])
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) "
            "VALUES('sports-user','sports_user','Sports User','active',?)",
            (timestamp,),
        )
        database.execute(
            "INSERT INTO flash_wallets(user_id,balance,created_at,updated_at) "
            "VALUES('sports-user',200,?,?)",
            (timestamp, timestamp),
        )

    def fake_report(_key, evidence, _user_id, *, actor):
        assert evidence["subject_type"] == "sports_game"
        return (
            {
                "headline": "HOM has the cleaner setup",
                "thesis": "The model prefers HOM, but the price leaves real uncertainty.",
                "summary": "Team form and the current market both support a close HOM lean.",
                "company_profile": {"what_it_does": "must not leak into sports"},
                "people": [{"name": "must not leak"}],
                "filings": [{"form": "must not leak"}],
                "catalysts": ["Home form is stronger."],
                "risks": ["The edge is small."],
                "watch": ["Watch the moneyline."],
                "unknowns": ["Lineups are not confirmed."],
                "sources": evidence["sources"],
                "citations": [],
            },
            actor.model,
            {},
        )

    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(web_main, "_generate_openrouter_report", fake_report)
    commission, created = web_main._create_research_commission(
        "sports-user", web_main._sports_report_key(str(event["id"]))
    )
    assert created is True
    assert commission["subject_type"] == "sports_game"
    assert commission["nav_product"] == "sports"

    report = web_main._run_research_commission(str(commission["id"]))
    assert report["status"] == "complete"
    assert report["ticker"] == "HOM"
    assert report["company"] == "Away Club at Home Club"
    assert report["company_profile"] == {}
    assert report["people"] == []
    assert report["filing_context"] == []
    daily = web_main.daily_report_for_sports_game(str(event["id"]), "sports-user")
    assert daily is not None
    assert daily["locked"] is False
    with connection() as database:
        balance = database.execute(
            "SELECT balance FROM flash_wallets WHERE user_id='sports-user'"
        ).fetchone()["balance"]
    assert balance == 100


def test_finished_game_seals_the_last_pregame_prediction_and_market(sports_db) -> None:
    start = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    pregame_at = start - timedelta(hours=1)
    postgame_at = start + timedelta(hours=3)

    pregame_raw = sample_event()
    pregame_raw["date"] = start.isoformat()
    pregame_event = normalize_event("mlb", pregame_raw)
    assert pregame_event is not None
    store_events([pregame_event], observed_at=pregame_at)

    with connection() as database:
        pregame_prediction = database.execute(
            "SELECT input_hash FROM sports_predictions WHERE event_id=?",
            (pregame_event["id"],),
        ).fetchone()
    assert pregame_prediction is not None

    final_raw = sample_event(completed=True)
    final_raw["date"] = start.isoformat()
    final_raw["competitions"][0]["competitors"][0]["records"][0]["summary"] = "61-40"
    final_raw["competitions"][0]["odds"][0]["moneyline"]["home"]["close"]["odds"] = "-170"
    final_raw["competitions"][0]["odds"][0]["moneyline"]["away"]["close"]["odds"] = "+145"
    final_event = normalize_event("mlb", final_raw)
    assert final_event is not None
    store_events([final_event], observed_at=postgame_at)

    detail = sports_event(str(final_event["id"]))
    assert detail is not None
    assert detail["prediction"]["observed_at"] == pregame_at.isoformat()
    assert detail["receipt"]["sealed"] is True
    assert detail["receipt"]["input_hash"] == pregame_prediction["input_hash"]
    assert detail["receipt"]["outcome"]["final_score"] == "AWY 3 – HOM 5"
    assert detail["odds"]["observed_at"] == pregame_at.isoformat()
    assert [row["observed_at"] for row in detail["odds_history"]] == [
        pregame_at.isoformat()
    ]

    response = sports_game_page(
        str(final_event["id"]),
        request(path=f"/game/{final_event['id']}"),
        None,
    )
    assert b"SEALED PREGAME" in response.body
    assert b"Later news and results cannot rewrite it" in response.body
    assert b"Pregame market timeline" in response.body


def test_sports_history_backfill_moves_through_older_chunks(
    sports_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def fake_range(league, start, end, *, feed):
        calls.append((league, start, end, feed))
        return []

    monkeypatch.setattr(sports_module, "_fetch_league_range", fake_range)
    current = datetime(2026, 8, 26, 20, tzinfo=UTC)

    assert fetch_league_history_chunk("mlb", current) == []
    assert fetch_league_history_chunk("mlb", current) == []
    assert calls[0][2] == current.date() - timedelta(days=2)
    assert calls[1][2] == calls[0][2] - timedelta(days=sports_module.HISTORY_CHUNK_DAYS)


def test_empty_player_boxscore_is_checked_only_once(
    sports_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = sample_event(completed=True)
    event = normalize_event("mlb", raw)
    assert event is not None
    store_events([event])
    monkeypatch.setattr(sports_module, "fetch_player_appearances", lambda _event: [])

    first = collect_stored_player_appearances("mlb")
    second = collect_stored_player_appearances("mlb")

    assert first["events"] == 1
    assert second["events"] == 0


def test_sports_host_has_the_same_alpha_product_as_runners(sports_db) -> None:
    radar_response = radar_page(request(path="/radar"), None)
    alpha_response = alpha_page(request(path="/alpha"), None)
    receipts_response = sports_receipts_page(request(path="/receipts"), None)
    assert radar_response.status_code == 200
    assert b"material change" in radar_response.body
    assert b'class="sports-hero' not in radar_response.body
    assert alpha_response.status_code == 200
    assert b'<h1>Alpha</h1>' in alpha_response.body
    assert b'class="alpha-board call-ledger"' in alpha_response.body
    assert b"open Calls" in alpha_response.body
    assert b"Receipts" not in alpha_response.body
    assert b'href="/alpha"' in alpha_response.body
    assert b'class="tab-link product-tab-link"' in alpha_response.body
    assert b">Runners</span>" in alpha_response.body
    assert receipts_response.status_code == 307
    assert receipts_response.headers["location"] == "/alpha"


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
