from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.requests import Request

from runner_web import db, golf_cup
from runner_web.main import sports_game_page
from runner_web.sports import golf_event, normalize_golf_event, store_golf_events

FIXTURE = Path(__file__).parent / "fixtures/golf_cup_2026.json"
NOW = datetime(2026, 9, 24, 3, 20, tzinfo=UTC)


@pytest.fixture
def sources():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def cup_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "cup.db")
    db.init_db()
    event = normalize_golf_event(
        {
            "id": "401824815",
            "name": "Presidents Cup",
            "date": "2026-09-24T16:35Z",
            "endDate": "2026-09-28T03:59Z",
            "status": {"type": {"state": "pre"}},
            "competitions": [
                {
                    "competitors": [
                        {"type": "team", "team": {"displayName": "USA"}, "score": "0"},
                        {"type": "team", "team": {"displayName": "International"}, "score": "0"},
                    ]
                }
            ],
        }
    )
    store_golf_events([event], observed_at=NOW)


def test_saved_evidence_joins_all_roster_players_and_published_pairs(sources):
    result = golf_cup.build_analysis(sources)
    assert [team["covered"] for team in result["teams"]] == [12, 12]
    assert len(result["matches"]) == 5
    first = result["matches"][0]
    assert first["teams"][0]["players"] == ["Scottie Scheffler", "Sam Burns"]
    assert first["teams"][1]["players"] == ["Sungjae Im", "Min Woo Lee"]
    assert first["start_time"] == "2026-09-24T16:35:00.000Z"
    prediction = result["prediction"]
    assert prediction["winner"] == "USA"
    assert 0.75 < prediction["usa"] < 0.85
    assert sum(prediction[key] for key in ["usa", "international", "tie"]) == pytest.approx(1)
    assert prediction["expected_usa"] + prediction["expected_international"] == pytest.approx(30)
    assert prediction["roster_average_matches"] == 25
    assert result == golf_cup.build_analysis(copy.deepcopy(sources))


def test_equal_strength_is_symmetric_and_preserves_a_tied_cup(sources):
    for player in sources["rankings"]["data"]["players"].values():
        player["average_points"] = 2
    prediction = golf_cup.build_analysis(sources)["prediction"]
    assert prediction["usa"] == pytest.approx(prediction["international"])
    assert prediction["tie"] > 0
    assert prediction["expected_usa"] == pytest.approx(15)
    assert prediction["winner"] == "Even matchup"


def test_stronger_players_raise_their_team_estimate(sources):
    original = golf_cup.build_analysis(sources)["prediction"]
    for name in sources["rosters"]["data"]["3"]:
        sources["rankings"]["data"]["players"][golf_cup._name(name)]["average_points"] *= 4
    stronger = golf_cup.build_analysis(sources)["prediction"]
    assert stronger["international"] > original["international"]
    assert stronger["usa"] < original["usa"]


@pytest.mark.parametrize("missing", ["rosters", "rankings", "matches"])
def test_incomplete_sources_keep_the_forecast_pending(sources, missing):
    del sources[missing]
    assert golf_cup.build_analysis(sources)["prediction"] is None


def test_missing_roster_player_keeps_pairings_and_holds_cup_estimate(sources):
    del sources["rankings"]["data"]["players"]["scottiescheffler"]
    result = golf_cup.build_analysis(sources)
    assert result["prediction"] is None
    assert len(result["matches"]) == 5
    assert result["teams"][0]["covered"] == 11


def test_pairing_identity_must_match_the_official_team(sources):
    sources["matches"]["data"]["matches"][0]["sides"]["1"]["players"] = ["Sungjae Im"]
    result = golf_cup.build_analysis(sources)
    assert result["matches"][0]["forecast"] is None
    assert result["prediction"] is None


@pytest.mark.parametrize(
    "points, winner", [({"1": 18.5, "3": 11.5}, "USA"), ({"1": 15, "3": 15}, "Cup tied")]
)
def test_finished_cup_uses_the_recorded_result(sources, points, winner):
    sources["matches"]["data"].update(points=points, completed=True, state="post")
    for match in sources["matches"]["data"]["matches"]:
        match.update(completed=True, state="post")
    prediction = golf_cup.build_analysis(sources)["prediction"]
    assert prediction["winner"] == winner
    assert prediction["winner_probability"] == 1
    assert prediction["remaining"] == 0
    assert prediction["expected_usa"] == points["1"]


def test_finished_matches_wait_for_consistent_team_totals(sources):
    sources["matches"]["data"]["matches"][0]["completed"] = True
    assert golf_cup.build_analysis(sources)["prediction"] is None
    sources["matches"]["data"]["points"] = {"1": 1, "3": 0}
    assert golf_cup.build_analysis(sources)["prediction"]["remaining"] == 29


def test_pairing_parser_preserves_names_times_and_partial_scores(sources):
    match = sources["matches"]["data"]["matches"][0]
    players = [
        {"teamId": side, "displayName": name, "score": "1 UP" if side == "1" else "1 DN"}
        for side in ["1", "3"]
        for name in match["sides"][side]["players"]
    ]
    payload = {
        "page": {
            "content": {
                "leaderboard": {
                    "mtch": {
                        "hdr": {
                            "uid": "s:1100~l:1106~e:401824815~c:12093",
                            "competitors": [
                                {"teamId": "1", "score": "0.5"},
                                {"teamId": "3", "score": "0.5"},
                            ],
                        },
                        "grps": [
                            {
                                "name": "Thursday Four-Balls",
                                "competitions": [
                                    {
                                        "id": "12094",
                                        "date": match["start_time"],
                                        "competitors": players,
                                        "status": {"state": "in", "detail": "Through 4"},
                                    }
                                ],
                            }
                        ],
                    }
                }
            }
        }
    }
    html = "<script>window['__espnfitt__']=" + json.dumps(payload) + ";</script>"
    result = golf_cup.parse_matches(html)
    assert result["points"] == {"1": 0.5, "3": 0.5}
    assert result["matches"][0]["sides"]["1"]["players"] == ["Scottie Scheffler", "Sam Burns"]
    assert result["matches"][0]["sides"]["1"]["score"] == "1 UP"
    assert result["matches"][0]["status"] == "Through 4"
    with pytest.raises(ValueError, match="Unexpected Cup event"):
        golf_cup.parse_matches(html.replace("e:401824815", "e:123"))
    with pytest.raises(ValueError, match="points are incomplete"):
        golf_cup.parse_matches(html.replace('"score": "0.5"', '"score": "—"'))


def test_official_roster_parser_joins_split_names_and_checks_columns(sources):
    cells = []
    for offset, label in [(0, "Automatic Qualifiers"), (6, "Captain’s Picks")]:
        cells.extend([f"2026 U.S. Team {label}", f"2026 INT Team {label}"])
        for index in range(offset, offset + 6):
            for team in ["1", "3"]:
                first, last = sources["rosters"]["data"][team][index].rsplit(" ", 1)
                cells.extend(["", first, last])
    html = "".join(f'<div data-valign="middle">{cell}</div>' for cell in cells)
    assert golf_cup.parse_rosters(html) == sources["rosters"]["data"]
    with pytest.raises(ValueError):
        golf_cup.parse_rosters(html.replace("2026 INT Team Automatic", "2026 Other Team Automatic"))


def test_ranking_parser_retains_the_source_date_and_rejects_invalid_points():
    payload = {
        "rankings": [
            {
                "type": "WORLDRANK",
                "update": "2026/09/20",
                "ranks": [
                    {
                        "athlete": {"displayName": "Scottie Scheffler", "id": "9478"},
                        "current": 1,
                        "recordStats": [{"name": "averagePoints", "value": 17.74}],
                    },
                    {
                        "athlete": {"displayName": "Invalid value"},
                        "current": 2,
                        "recordStats": [{"name": "averagePoints", "value": float("nan")}],
                    },
                ],
            }
        ]
    }
    result = golf_cup.parse_rankings(payload)
    assert result["updated_at"] == "2026-09-20"
    assert list(result["players"]) == ["scottiescheffler"]
    assert result["players"]["scottiescheffler"]["average_points"] == 17.74


def test_collection_preserves_history_and_source_times_after_failure(cup_db, sources, monkeypatch):
    receipts = []
    calls = []

    def fetch(key, url, parser, now):
        calls.append(key)
        return copy.deepcopy(sources[key])

    monkeypatch.setattr(golf_cup, "_fetch_source", fetch)
    monkeypatch.setattr(golf_cup, "record_source_fetch", receipts.append)
    assert golf_cup.refresh_cup_analysis(NOW)["players"] == 24
    assert len(receipts) == 3
    saved = golf_cup.latest_snapshot()
    assert (
        saved["analysis"]["prediction"] == golf_cup.build_analysis(saved["sources"])["prediction"]
    )
    assert golf_event(golf_cup.EVENT_ID)["analysis"]["matches"]

    def fail(key, url, parser, now):
        raise TimeoutError("Source timed out")

    monkeypatch.setattr(golf_cup, "_fetch_source", fail)
    golf_cup.refresh_cup_analysis(NOW + timedelta(minutes=31))
    later = golf_cup.saved_cup_analysis(NOW + timedelta(minutes=31))
    assert later["stale"] is True
    assert later["refresh_pending"] is True
    assert later["prediction"] == saved["analysis"]["prediction"]
    assert later["match_time"] == NOW.isoformat()
    assert receipts[-1].status == "error"
    assert receipts[-1].locator == golf_cup.MATCH_URL
    with db.connection() as database:
        assert (
            database.execute("SELECT COUNT(*) AS n FROM sports_golf_analysis").fetchone()["n"] == 2
        )


def test_recent_sources_are_reused_without_network_calls(cup_db, sources, monkeypatch):
    monkeypatch.setattr(golf_cup, "_fetch_source", lambda key, *args: copy.deepcopy(sources[key]))
    monkeypatch.setattr(golf_cup, "record_source_fetch", lambda _: None)
    golf_cup.refresh_cup_analysis(NOW)
    monkeypatch.setattr(
        golf_cup, "_fetch_source", lambda *args: pytest.fail("cached source fetched")
    )
    golf_cup.refresh_cup_analysis(NOW + timedelta(minutes=1))
    assert golf_cup.saved_cup_analysis(NOW + timedelta(minutes=1))["stale"] is False


def test_cup_route_renders_the_saved_forecast_and_all_players(cup_db, sources, monkeypatch):
    monkeypatch.setattr(golf_cup, "_fetch_source", lambda key, *args: copy.deepcopy(sources[key]))
    monkeypatch.setattr(golf_cup, "record_source_fetch", lambda _: None)
    golf_cup.refresh_cup_analysis(NOW)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": f"/game/{golf_cup.EVENT_ID}",
            "query_string": b"",
            "headers": [(b"host", b"sports.rati.chat")],
            "server": ("sports.rati.chat", 443),
        }
    )
    response = sports_game_page(golf_cup.EVENT_ID, request, None)
    assert response.status_code == 200
    html = response.body.decode()
    assert "Expected final points" in html
    assert "80.8%" in html
    assert 'aria-label="Choose an outcome"' in html
    assert "Winner · half on a tie" in html
    for name in sources["rosters"]["data"]["1"] + sources["rosters"]["data"]["3"]:
        assert name in html
    assert "data-live-surface" in html
