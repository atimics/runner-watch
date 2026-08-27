from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from runner_web import db, odds_api, sports
from runner_web.db import connection, init_db
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


def test_preferred_bookmaker_provides_a_complete_two_sided_line() -> None:
    lines = normalize_moneylines(api_payload(), ("preferred-book",))
    assert len(lines) == 1
    assert lines[0]["sportsbook"] == "Preferred Book"
    assert lines[0]["home_odds"] == -130
    assert lines[0]["away_odds"] == 115


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

    result = fetch_moneylines(OddsApiConfig(api_key="very-secret"), "mlb")

    assert isinstance(result, OddsFetchResult)
    assert "very-secret" in captured_request["url"]
    assert "very-secret" not in result.locator
    assert "very-secret" not in recorded_fetches[0].locator
    assert "markets=h2h" in result.locator
    assert "regions=us" in result.locator


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
    event = normalize_event("mlb", raw_event())
    assert event is not None
    lines = normalize_moneylines(api_payload(), ("preferred-book",))
    assert apply_moneylines([event], lines) == 1
    store_events([event], observed_at=datetime(2026, 8, 26, 18, tzinfo=UTC))

    with connection() as database:
        row = database.execute("SELECT * FROM sports_odds_snapshots").fetchone()
    assert row["provider"] == "the-odds-api"
    assert row["sportsbook"] == "Preferred Book"
    assert row["home_open_odds"] == -130
    assert row["away_open_odds"] == 115

    clear_event_odds([event])
    assert _apply_cached_moneylines([event]) == 1
    assert event["odds_provider"] == "the-odds-api"
    assert event["home_odds"] == -130


def test_sports_refresh_uses_one_paid_moneyline_call_per_due_league(
    sports_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "server-only-secret")
    now = datetime(2026, 8, 26, 20, tzinfo=UTC)
    lines = normalize_moneylines(api_payload(), ("preferred-book",))
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

    result = sports.refresh_sports(now)

    assert spent["credits"] == 4
    assert result["odds_api"]["credits_used"] == 4
    assert result["odds_api"]["fresh_matches"] == {
        "mlb": 1,
        "nfl": 1,
        "nba": 1,
        "nhl": 1,
    }
    with connection() as database:
        providers = database.execute(
            "SELECT provider,COUNT(*) AS count FROM sports_odds_snapshots GROUP BY provider"
        ).fetchall()
    assert [(row["provider"], row["count"]) for row in providers] == [("the-odds-api", 4)]
