from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request

from runner_web import db, odds_api, sports
from runner_web.db import connection, init_db
from runner_web.main import sports_game_page
from runner_web.odds_api import (
    OddsApiConfig,
    OddsApiError,
    OddsFetchResult,
    Quota,
    apply_moneylines,
    can_spend,
    clear_event_odds,
    fetch_moneylines,
    normalize_moneylines,
    refresh_decision,
)
from runner_web.sports import _apply_cached_moneylines, normalize_event, store_events

ODDS_OBSERVED_AT = datetime(2026, 8, 26, 18, 10, tzinfo=UTC)


def raw_event() -> dict[str, Any]:
    return {
        "id": "401000001",
        "name": "Away Club at Home Club",
        "season": {"slug": "regular-season"},
        "date": "2026-08-27T02:00:00Z",
        "status": {"type": {"state": "pre", "completed": False, "shortDetail": "7 PM"}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "score": "0",
                        "team": {"id": "1", "displayName": "Home Club", "abbreviation": "HOM"},
                        "records": [{"name": "overall", "summary": "60-40"}],
                    },
                    {
                        "homeAway": "away",
                        "score": "0",
                        "team": {"id": "2", "displayName": "Away Club", "abbreviation": "AWY"},
                        "records": [{"name": "overall", "summary": "48-52"}],
                    },
                ],
                "odds": [],
            }
        ],
    }


def api_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": "odds-event-1",
            "home_team": "Home Club",
            "away_team": "Away Club",
            "commence_time": "2026-08-27T02:00:00Z",
            "bookmakers": [
                {
                    "key": "newer-book",
                    "title": "Newer Book",
                    "last_update": "2026-08-26T18:05:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Home Club", "price": -135},
                                {"name": "Away Club", "price": 120},
                            ],
                        }
                    ],
                },
                {
                    "key": "preferred-book",
                    "title": "Preferred Book",
                    "last_update": "2026-08-26T18:00:00Z",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Home Club", "price": -130},
                                {"name": "Away Club", "price": 115},
                            ],
                        }
                    ],
                },
            ],
        }
    ]


def multi_book_payload() -> list[dict[str, Any]]:
    def bookmaker(
        key: str,
        title: str,
        home_odds: int,
        away_odds: int,
        updated: str,
    ) -> dict[str, Any]:
        return {
            "key": key,
            "title": title,
            "last_update": updated,
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": "Home Club", "price": home_odds},
                        {"name": "Away Club", "price": away_odds},
                    ],
                }
            ],
        }

    return [
        {
            "id": "odds-event-1",
            "home_team": "Home Club",
            "away_team": "Away Club",
            "commence_time": "2026-08-27T02:00:00Z",
            "bookmakers": [
                bookmaker("bovada", "Bovada", 110, -130, "2026-08-26T18:05:00Z"),
                bookmaker("draftkings", "DraftKings", -125, 110, "2026-08-26T18:04:00Z"),
                bookmaker("fanduel", "FanDuel", -120, 105, "2026-08-26T18:03:00Z"),
                bookmaker("betmgm", "BetMGM", -118, 102, "2026-08-26T18:02:00Z"),
                bookmaker(
                    "betonlineag",
                    "BetOnline.ag",
                    -122,
                    106,
                    "2026-08-26T18:01:00Z",
                ),
                bookmaker("stale", "Stale Book", -115, 100, "2026-08-26T17:00:00Z"),
            ],
        }
    ]


def sports_request(path: str) -> Request:
    return Request(
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


@pytest.fixture
def sports_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "odds-api.db")
    init_db()


def test_budget_stops_at_working_limit_or_reserve() -> None:
    config = OddsApiConfig(api_key="secret", working_limit=450, reserve_credits=50)
    assert can_spend(config, Quota(used=449, remaining=51, last=0))
    assert not can_spend(config, Quota(used=450, remaining=50, last=1))
    assert not can_spend(config, Quota(used=440, remaining=50, last=1))


def test_config_refuses_more_than_one_credit_of_bookmakers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "secret")
    monkeypatch.setenv("ODDS_API_BOOKMAKERS", ",".join(f"book-{index}" for index in range(11)))
    with pytest.raises(ValueError, match="at most 10"):
        OddsApiConfig.from_env()


def test_config_selects_multi_book_feed_and_prefers_bovada(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "secret")
    monkeypatch.setenv("ODDS_API_BOOKMAKERS", "bovada,draftkings,fanduel,betmgm,betonlineag")
    monkeypatch.setenv("ODDS_API_PREFERRED_BOOKMAKERS", "bovada")

    config = OddsApiConfig.from_env()

    assert config.bookmakers == ("bovada", "draftkings", "fanduel", "betmgm", "betonlineag")
    assert config.preferred_bookmakers == ("bovada",)


def test_preferred_bookmaker_provides_a_complete_two_sided_line() -> None:
    lines = normalize_moneylines(api_payload(), ("preferred-book",), ODDS_OBSERVED_AT)
    assert len(lines) == 1
    assert lines[0]["preferred"]["sportsbook"] == "Preferred Book"
    assert lines[0]["preferred"]["home_odds"] == -130
    assert lines[0]["preferred"]["away_odds"] == 115
    assert lines[0]["consensus"] is None


def test_fresh_multi_book_lines_build_median_consensus_and_exclude_stale_book() -> None:
    markets = normalize_moneylines(multi_book_payload(), ("bovada",), ODDS_OBSERVED_AT)

    assert len(markets) == 1
    market = markets[0]
    assert market["preferred"]["sportsbook"] == "Bovada"
    assert len(market["bookmakers"]) == 5
    assert {line["sportsbook_key"] for line in market["bookmakers"]} == {
        "bovada",
        "draftkings",
        "fanduel",
        "betmgm",
        "betonlineag",
    }
    assert market["consensus"]["sportsbook"] == "Market consensus"
    assert market["consensus"]["bookmaker_count"] == 5
    assert market["consensus"]["home_probability"] == pytest.approx(0.5279, abs=0.002)


def test_all_old_bookmaker_lines_are_rejected_even_when_they_agree() -> None:
    observed_at = ODDS_OBSERVED_AT + timedelta(hours=3)

    markets = normalize_moneylines(multi_book_payload(), ("bovada",), observed_at)

    assert markets == ()


def test_same_team_series_matches_each_market_by_start_time_once() -> None:
    first_raw = raw_event()
    second_raw = raw_event()
    second_raw["id"] = "401000002"
    second_raw["date"] = "2026-08-27T06:00:00Z"
    first = normalize_event("mlb", first_raw)
    second = normalize_event("mlb", second_raw)
    assert first is not None and second is not None

    payload = api_payload()
    second_market = json.loads(json.dumps(payload[0]))
    second_market["id"] = "odds-event-2"
    second_market["commence_time"] = "2026-08-27T06:00:00Z"
    second_market["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = -175
    second_market["bookmakers"][1]["markets"][0]["outcomes"][0]["price"] = -170
    markets = normalize_moneylines(
        [second_market, payload[0]],
        ("preferred-book",),
        ODDS_OBSERVED_AT,
    )

    assert apply_moneylines([first, second], markets) == 2
    assert first["home_odds"] == -130
    assert second["home_odds"] == -170


def test_one_market_is_not_reused_for_another_game_in_the_series() -> None:
    first_raw = raw_event()
    second_raw = raw_event()
    second_raw["id"] = "401000002"
    second_raw["date"] = "2026-08-27T06:00:00Z"
    first = normalize_event("mlb", first_raw)
    second = normalize_event("mlb", second_raw)
    assert first is not None and second is not None
    markets = normalize_moneylines(api_payload(), ("preferred-book",), ODDS_OBSERVED_AT)

    assert apply_moneylines([first, second], markets) == 1
    assert first["home_odds"] == -130
    assert second.get("odds_provider") != odds_api.PROVIDER


def test_refresh_schedule_uses_three_progressive_game_windows() -> None:
    start = datetime(2026, 8, 27, 2, tzinfo=UTC)
    events = [{"status": "pre", "start_time": start}]

    opening = refresh_decision("mlb", events, start - timedelta(hours=30), state={})
    assert opening is not None and opening.slot == "opening"

    pregame = refresh_decision(
        "mlb",
        events,
        start - timedelta(hours=5),
        state={"slate": "2026-08-26", "completed": ["opening"]},
    )
    assert pregame is not None and pregame.slot == "pregame"

    close = refresh_decision(
        "mlb",
        events,
        start - timedelta(hours=1),
        state={"slate": "2026-08-26", "completed": ["opening", "pregame"]},
    )
    assert close is not None and close.slot == "close"

    assert (
        refresh_decision(
            "mlb",
            events,
            start - timedelta(minutes=30),
            state={"slate": "2026-08-26", "completed": ["opening", "pregame", "close"]},
        )
        is None
    )


def test_paid_request_logs_only_a_sanitized_locator(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_request: dict[str, str] = {}
    recorded_fetches: list[Any] = []

    class Response:
        headers = {
            "x-requests-used": "1",
            "x-requests-remaining": "499",
            "x-requests-last": "1",
        }

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(api_payload()).encode()

    def fake_open(request: Any, timeout: int) -> Response:
        captured_request["url"] = request.full_url
        assert timeout == 20
        return Response()

    monkeypatch.setattr(odds_api.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(odds_api, "record_source_fetch", recorded_fetches.append)
    monkeypatch.setattr(odds_api, "record_quota", lambda _quota: None)

    result = fetch_moneylines(
        OddsApiConfig(
            api_key="very-secret",
            bookmakers=("bovada",),
            preferred_bookmakers=("bovada",),
        ),
        "mlb",
    )

    assert isinstance(result, OddsFetchResult)
    assert "very-secret" in captured_request["url"]
    assert "very-secret" not in result.locator
    assert "very-secret" not in recorded_fetches[0].locator
    assert "markets=h2h" in result.locator
    assert "bookmakers=bovada" in result.locator
    assert "regions=" not in result.locator


def test_billed_bad_response_carries_quota_to_prevent_retry_spend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        headers = {
            "x-requests-used": "450",
            "x-requests-remaining": "50",
            "x-requests-last": "1",
        }

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"not-json"

    monkeypatch.setattr(odds_api.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(odds_api, "record_source_fetch", lambda _fetch: None)
    monkeypatch.setattr(odds_api, "record_quota", lambda _quota: None)

    with pytest.raises(OddsApiError) as error:
        fetch_moneylines(OddsApiConfig(api_key="very-secret"), "mlb")

    assert error.value.quota == Quota(used=450, remaining=50, last=1)


def test_api_odds_keep_provider_and_opening_snapshot(sports_db: None) -> None:
    observed_at = datetime.now(UTC)
    start_time = observed_at + timedelta(hours=4)
    raw = raw_event()
    raw["date"] = start_time.isoformat()
    event = normalize_event("mlb", raw)
    assert event is not None
    payload = api_payload()
    payload[0]["commence_time"] = start_time.isoformat()
    for offset, bookmaker in enumerate(payload[0]["bookmakers"]):
        bookmaker["last_update"] = (observed_at - timedelta(minutes=offset + 1)).isoformat()
    lines = normalize_moneylines(payload, ("preferred-book",), observed_at)
    assert apply_moneylines([event], lines) == 1
    store_events([event], observed_at=observed_at)

    with connection() as database:
        row = database.execute("SELECT * FROM sports_odds_snapshots").fetchone()
        bookmaker_count = database.execute(
            "SELECT COUNT(*) AS count FROM sports_bookmaker_odds"
        ).fetchone()["count"]
    assert row["provider"] == "the-odds-api"
    assert row["sportsbook"] == "Preferred Book"
    assert row["home_open_odds"] == -130
    assert row["away_open_odds"] == 115
    assert bookmaker_count == 2

    clear_event_odds([event])
    assert _apply_cached_moneylines([event]) == 1
    assert event["odds_provider"] == "the-odds-api"
    assert event["home_odds"] == -130


def test_multi_book_consensus_drives_model_and_bovada_drives_paper_pick(
    sports_db: None,
) -> None:
    observed_at = datetime.now(UTC)
    start_time = observed_at + timedelta(hours=4)
    raw = raw_event()
    raw["date"] = start_time.isoformat()
    event = normalize_event("mlb", raw)
    assert event is not None
    payload = multi_book_payload()
    payload[0]["commence_time"] = start_time.isoformat()
    for offset, bookmaker in enumerate(payload[0]["bookmakers"]):
        age = timedelta(hours=3) if bookmaker["key"] == "stale" else timedelta(minutes=offset + 1)
        bookmaker["last_update"] = (observed_at - age).isoformat()
    markets = normalize_moneylines(payload, ("bovada",), observed_at)

    assert apply_moneylines([event], markets) == 1
    assert event["sportsbook"] == "Market consensus"
    assert event["market_book_count"] == 5
    assert event["market_is_consensus"] is True
    store_events([event], observed_at=observed_at)

    with connection() as database:
        snapshot = database.execute("SELECT * FROM sports_odds_snapshots").fetchone()
        quote_count = database.execute(
            "SELECT COUNT(*) AS count FROM sports_bookmaker_odds"
        ).fetchone()["count"]
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) "
            "VALUES('multi-user','multi_member','Multi Member','active',?)",
            (datetime.now(UTC).isoformat(),),
        )

    assert snapshot["sportsbook"] == "Market consensus"
    assert quote_count == 5
    detail = sports.sports_event(str(event["id"]))
    assert detail is not None
    comparison = detail["market_comparison"]
    assert comparison["enough_books"] is True
    assert comparison["book_count"] == 5
    assert comparison["bovada"]["material"] is True
    assert comparison["bovada"]["divergence_team"] == "AWY"
    assert comparison["bovada"]["comparison_book_count"] == 4
    assert "Fresh market consensus: 5 sportsbooks." in detail["prediction"]["evidence"]

    response = sports_game_page(
        str(event["id"]),
        sports_request(f"/game/{event['id']}"),
        None,
    )
    assert response.status_code == 200
    assert b"Consensus and Bovada" in response.body
    assert b"BOVADA DIVERGENCE" in response.body
    assert b"BEST DISPLAYED PRICE" in response.body
    assert b"Pricing differences are not picks" in response.body

    pick = sports.create_sports_pick("multi-user", str(event["id"]), "home")
    assert pick["sportsbook"] == "Bovada"
    assert pick["american_odds"] == 110
    assert pick["odds_observed_at"] == observed_at.isoformat()


def test_consensus_line_keeps_paper_picks_available_without_bovada(
    sports_db: None,
) -> None:
    observed_at = datetime.now(UTC)
    start_time = observed_at + timedelta(hours=4)
    raw = raw_event()
    raw["date"] = start_time.isoformat()
    event = normalize_event("mlb", raw)
    assert event is not None
    payload = multi_book_payload()
    payload[0]["commence_time"] = start_time.isoformat()
    payload[0]["bookmakers"] = [
        bookmaker
        for bookmaker in payload[0]["bookmakers"]
        if bookmaker["key"] not in {"bovada", "stale"}
    ]
    for offset, bookmaker in enumerate(payload[0]["bookmakers"]):
        bookmaker["last_update"] = (observed_at - timedelta(minutes=offset + 1)).isoformat()
    markets = normalize_moneylines(payload, ("bovada",), observed_at)

    assert apply_moneylines([event], markets) == 1
    store_events([event], observed_at=observed_at)
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) "
            "VALUES('fallback-user','fallback_member','Fallback Member','active',?)",
            (observed_at.isoformat(),),
        )

    detail = sports.sports_event(str(event["id"]))
    assert detail is not None
    assert detail["market_comparison"]["bovada"] is None
    assert detail["paper_odds"]["sportsbook"] == "Market consensus"

    pick = sports.create_sports_pick("fallback-user", str(event["id"]), "home")
    assert pick["sportsbook"] == "Market consensus"
    assert pick["american_odds"] == detail["paper_odds"]["home_odds"]


def test_sports_refresh_uses_one_paid_moneyline_call_per_due_league(
    sports_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "server-only-secret")
    now = datetime(2026, 8, 26, 20, tzinfo=UTC)
    lines = normalize_moneylines(api_payload(), ("preferred-book",), ODDS_OBSERVED_AT)
    spent = {"credits": 0}

    def fake_league(league: str, _at: datetime | None = None) -> list[dict[str, Any]]:
        event = normalize_event(league, raw_event())
        assert event is not None
        return [event]

    def fake_moneylines(_config: OddsApiConfig, _league: str) -> OddsFetchResult:
        spent["credits"] += 1
        return OddsFetchResult(
            moneylines=lines,
            quota=Quota(
                used=spent["credits"],
                remaining=500 - spent["credits"],
                last=1,
            ),
            locator="https://api.the-odds-api.com/v4/sports/test/odds?markets=h2h",
        )

    monkeypatch.setattr(sports, "fetch_league", fake_league)
    monkeypatch.setattr(sports, "fetch_league_history_chunk", lambda *_args: [])
    monkeypatch.setattr(sports, "fetch_moneylines", fake_moneylines)
    monkeypatch.setattr(sports, "probe_quota", lambda _config: Quota(0, 500, 0))
    monkeypatch.setattr(
        sports,
        "collect_player_appearances",
        lambda _events: {"events": 0, "players": 0, "errors": 0},
    )
    monkeypatch.setattr(sports, "fetch_league_news", lambda *_args: [])
    monkeypatch.setattr(sports, "fetch_golf", lambda *_args: [])

    result = sports.refresh_sports(now)

    assert spent["credits"] == 4
    assert result["odds_api"]["credits_used"] == 4
    assert result["odds_api"]["fresh_matches"] == {
        "mlb": 1,
        "nfl": 1,
        "nba": 1,
        "nhl": 1,
    }
    assert result["golf"] == {"events": 0, "entrants": 0, "error": None}
    with connection() as database:
        providers = database.execute(
            "SELECT provider,COUNT(*) AS count FROM sports_odds_snapshots GROUP BY provider"
        ).fetchall()
    assert [(row["provider"], row["count"]) for row in providers] == [("the-odds-api", 4)]
