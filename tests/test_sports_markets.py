from __future__ import annotations

from datetime import UTC, datetime

import pytest

from runner_web import db, sports_markets
from runner_web.sports import normalize_event, store_events

AT = datetime(2026, 9, 24, 12, tzinfo=UTC)
START = "2026-09-25T00:15:00Z"


def game(*, game_id="123", start=START, away="ATL", home="GB"):
    raw = {
        "id": game_id,
        "name": "Atlanta Falcons at Green Bay Packers",
        "season": {"slug": "regular-season"},
        "date": start,
        "status": {"type": {"state": "pre", "completed": False}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "away",
                        "team": {"id": "1", "displayName": "Atlanta Falcons", "abbreviation": away},
                    },
                    {
                        "homeAway": "home",
                        "team": {
                            "id": "2",
                            "displayName": "Green Bay Packers",
                            "abbreviation": home,
                        },
                    },
                ]
            }
        ],
    }
    event = normalize_event("nfl", raw)
    assert event is not None
    return event


def kalshi_event():
    return {
        "series_ticker": "KXNFLGAME",
        "event_ticker": "KXNFLGAME-26SEP24ATLGB",
        "sub_title": "ATL vs GB (Sep 24)",
        "product_metadata": {"competition_scope": "Game", "competition": "Pro Football"},
        "markets": [
            {
                "ticker": "KXNFLGAME-26SEP24ATLGB-GB",
                "status": "active",
                "market_type": "binary",
                "title": "Green Bay wins",
                "yes_bid_dollars": "0.30",
                "yes_ask_dollars": "0.32",
                "updated_time": "2026-09-24T11:55:00Z",
            },
            {
                "ticker": "KXNFLGAME-26SEP24ATLGB-ATL",
                "status": "active",
                "market_type": "binary",
                "title": "Atlanta wins",
                "yes_bid_dollars": "0.68",
                "yes_ask_dollars": "0.70",
                "updated_time": "2026-09-24T11:55:00Z",
            },
        ],
    }


def polymarket_event():
    return {
        "id": "venue-123",
        "slug": "nfl-atl-gb-2026-09-25",
        "startTime": START,
        "active": True,
        "closed": False,
        "markets": [
            {
                "id": "market-123",
                "sportsMarketType": "moneyline",
                "active": True,
                "closed": False,
                "outcomes": '["Packers", "Falcons"]',
                "outcomePrices": '["0.32", "0.68"]',
                "updatedAt": "2026-09-24T11:50:00Z",
            }
        ],
    }


def test_kalshi_matches_full_game_and_outcome_sides():
    event = game()
    reading = sports_markets.normalize_kalshi([event], [kalshi_event()], AT)
    assert len(reading) == 1
    assert reading[0]["away_probability"] == pytest.approx(0.69)
    assert reading[0]["home_probability"] == pytest.approx(0.31)
    assert reading[0]["source"] == "kalshi"
    assert "bid/ask midpoint" in reading[0]["price_basis"]
    other_game = game(game_id="456", start="2026-09-25T03:15:00Z")
    assert sports_markets.normalize_kalshi([event, other_game], [kalshi_event()], AT) == []
    wrong = kalshi_event()
    wrong["product_metadata"] = {"competition_scope": "Season"}
    assert sports_markets.normalize_kalshi([event], [wrong], AT) == []


def test_polymarket_uses_only_exact_moneyline_and_kickoff():
    event = game()
    reading = sports_markets.normalize_polymarket([event], [polymarket_event()], AT)
    assert len(reading) == 1
    assert reading[0]["away_probability"] == pytest.approx(0.68)
    assert reading[0]["home_probability"] == pytest.approx(0.32)
    assert reading[0]["source_market_id"] == "market-123"
    props = polymarket_event()
    props["slug"] += "-player-props"
    spread = polymarket_event()
    spread["markets"][0]["sportsMarketType"] = "spreads"
    late = polymarket_event()
    late["startTime"] = "2026-09-25T06:15:00Z"
    assert sports_markets.normalize_polymarket([event], [props, spread, late], AT) == []
    assert (
        sports_markets.normalize_polymarket(
            [event], [polymarket_event()], datetime(2026, 9, 25, 1, tzinfo=UTC)
        )
        == []
    )


def test_exact_polymarket_slug_batch_avoids_prop_events(monkeypatch):
    urls = []
    monkeypatch.setattr(sports_markets, "_get_json", lambda url: urls.append(url) or [])
    sports_markets.fetch_polymarket([game()])
    assert len(urls) == 1
    assert "slug=nfl-atl-gb-2026-09-24" in urls[0]
    assert "slug=nfl-atl-gb-2026-09-25" in urls[0]
    assert "player-props" not in urls[0]


def test_saved_venue_readings_keep_sources_and_price_changes_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "sports.db")
    db.init_db()
    event = game()
    store_events([event], observed_at=AT)
    kalshi = sports_markets.normalize_kalshi([event], [kalshi_event()], AT)[0]
    poly = sports_markets.normalize_polymarket([event], [polymarket_event()], AT)[0]
    assert sports_markets.store_readings([kalshi, poly]) == 2
    assert sports_markets.store_readings([kalshi, poly]) == 0
    changed = {
        **poly,
        "home_probability": 0.3,
        "away_probability": 0.7,
        "quote_hash": "later",
        "source_updated_at": "2026-09-24T13:00:00+00:00",
        "observed_at": "2026-09-24T13:01:00+00:00",
    }
    assert sports_markets.store_readings([changed]) == 1
    latest, history = sports_markets.event_readings(event["id"], event["start_time"].isoformat())
    assert {item["source"] for item in latest} == {"kalshi", "polymarket"}
    assert len(history["kalshi"]) == 1
    assert len(history["polymarket"]) == 2
    assert (
        next(item for item in latest if item["source"] == "polymarket")["away_probability"] == 0.7
    )


def test_polymarket_mlb_full_team_names():
    event = game(game_id="mlb-1", away="NYM", home="WSH")
    event["league"] = "mlb"
    event["away"]["name"] = "New York Mets"
    event["home"]["name"] = "Washington Nationals"
    raw = polymarket_event()
    raw["slug"] = "mlb-nym-wsh-2026-09-25"
    raw["markets"][0]["outcomes"] = '["Washington Nationals", "New York Mets"]'
    reading = sports_markets.normalize_polymarket([event], [raw], AT)
    assert len(reading) == 1
    assert reading[0]["away_probability"] == pytest.approx(0.68)
    assert reading[0]["home_probability"] == pytest.approx(0.32)


def test_game_detail_labels_each_venue_and_model_gap(tmp_path, monkeypatch):
    from datetime import timedelta

    from runner_web.market_screens import detail
    from runner_web.sports import sports_event

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "sports.db")
    db.init_db()
    now = datetime.now(UTC)
    event = game(start=(now + timedelta(hours=4)).isoformat())
    store_events([event], observed_at=now)
    readings = [
        sports_markets._reading(
            event=event,
            source=source,
            source_event_id=f"{source}-event",
            market_id=f"{source}-market",
            away=chance,
            home=1 - chance,
            source_updated_at=(now - timedelta(minutes=2)).isoformat(),
            source_url=f"https://example.test/{source}",
            at=now,
            price_basis="Test price",
        )
        for source, chance in (("kalshi", 0.6), ("polymarket", 0.7))
    ]
    assert sports_markets.store_readings(readings) == 2
    game_detail = sports_event(event["id"])
    assert game_detail is not None
    screen = detail("sports", game_detail)
    assert {row["source"] for row in screen["prediction_markets"]} == {
        "Kalshi", "Polymarket"
    }
    assert {row["chance"] for row in screen["prediction_markets"]} == {30.0, 40.0}
    assert all(row["gap_pp"] is not None for row in screen["prediction_markets"])
