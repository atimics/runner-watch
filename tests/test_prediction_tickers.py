from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from runner_web import db, golf_cup, golf_markets, sports_markets
from runner_web.prediction_tickers import ticker
from runner_web.sports import normalize_event, sports_event, store_events

AT = datetime(2026, 9, 24, 12, tzinfo=UTC)


def team_event():
    return {
        "id": "nfl:123",
        "league": "nfl",
        "start_time": (AT + timedelta(minutes=10)).isoformat(),
        "home_abbreviation": "GB",
        "away_abbreviation": "ATL",
        "status": "pre",
        "prediction": {
            "model_version": "team-form-v1",
            "observed_at": AT.isoformat(),
            "home_probability": 0.5914,
            "away_probability": 0.4086,
            "home_market_probability": 0.6846,
            "away_market_probability": 0.3154,
        },
    }


def cup_event():
    sources = json.loads((Path(__file__).parent / "fixtures/golf_cup_2026.json").read_text())
    return {
        "id": golf_cup.EVENT_ID,
        "start_time": AT.isoformat(),
        "analysis": {**golf_cup.build_analysis(sources), "captured_at": AT.isoformat()},
    }


def quote(
    source="kalshi", *, at=AT, probability=0.85, contract="winner", outcome="usa", quality="quoted"
):
    return {
        "event_id": golf_cup.EVENT_ID,
        "contract_key": contract,
        "outcome_key": outcome,
        "source": source,
        "probability": probability,
        "observed_at": at.isoformat(),
        "quality": quality,
    }


def test_one_selected_outcome_keeps_favorite_and_value_comparisons_clear():
    event = team_event()
    result = ticker(event, now=AT)
    assert result["symbol"] == "NFL:123"
    assert result["selected"]["label"] == "GB"
    assert result["selected"]["percent"] == 59.1
    assert result["selected"]["benchmark"]["percent"] == 68.5
    assert result["selected"]["gap"] == -9.3
    other = ticker(event, outcome="away", now=AT)["selected"]
    assert other["label"] == "ATL"
    assert other["percent"] == 40.9
    assert other["gap"] == 9.3


def test_venue_lines_keep_independent_timestamps_and_model_version():
    event = team_event()
    event["prediction_history"] = [
        {**event["prediction"], "observed_at": (AT - timedelta(minutes=20)).isoformat()},
        event["prediction"],
        {**event["prediction"], "model_version": "old-model", "home_probability": 0.8},
        {**event["prediction"], "observed_at": (AT + timedelta(hours=1)).isoformat()},
    ]
    event["prediction_market_history"] = {
        source: [
            {
                "source": source,
                "home_probability": chance,
                "away_probability": 1 - chance,
                "observed_at": (AT - timedelta(minutes=minutes)).isoformat(),
            }
        ]
        for source, chance, minutes in [("kalshi", 0.7, 10), ("polymarket", 0.65, 5)]
    }
    result = ticker(event, now=AT)["selected"]
    assert [v["label"] for v in result["venues"]] == ["Kalshi", "Polymarket", "Sportsbook"]
    lines = {line["key"]: line for line in result["chart"]["series"]}
    assert set(lines) == {"rati", "sportsbook", "kalshi", "polymarket"}
    assert lines["rati"]["count"] == 2
    assert lines["rati"]["dots"][0][0] < lines["kalshi"]["dots"][0][0]
    assert lines["kalshi"]["dots"][0][0] < lines["polymarket"]["dots"][0][0]
    assert lines["polymarket"]["dots"][0][0] < lines["rati"]["dots"][-1][0]


def test_cup_tie_payout_uses_a_separate_contract_and_fair_value():
    event = cup_event()
    event["contract_quotes"] = [quote(), quote("polymarket", contract="winner-half-tie")]
    outright = ticker(event, now=AT)
    shared = ticker(event, contract="winner-half-tie", now=AT)
    probabilities = {o["key"]: o["model"] for o in outright["contract"]["outcomes"]}
    assert sum(probabilities.values()) == pytest.approx(1)
    assert shared["selected"]["model"] == pytest.approx(
        probabilities["usa"] + probabilities["tie"] / 2
    )
    assert [v["source"] for v in outright["selected"]["venues"]] == ["kalshi"]
    assert [v["source"] for v in shared["selected"]["venues"]] == ["polymarket"]
    assert "17.4" in outright["score_call"]["value"]
    assert shared["contract"]["unit"] == "Fair value"


@pytest.mark.parametrize("kind", ["stale_quote", "stale_model", "wide_spread", "missing_model"])
def test_gap_requires_current_model_and_usable_quote(kind):
    event = cup_event()
    event["contract_quotes"] = [quote()]
    if kind == "stale_quote":
        event["contract_quotes"][0]["observed_at"] = (AT - timedelta(hours=1)).isoformat()
    elif kind == "stale_model":
        event["analysis"]["stale"] = True
    elif kind == "wide_spread":
        event["contract_quotes"][0]["quality"] = "wide spread"
    else:
        event["analysis"]["prediction"] = None
    selected = ticker(event, now=AT)["selected"]
    assert selected["gap"] is None
    assert selected["venues"][0]["percent"] == 85


def test_cup_sources_are_matched_by_event_and_settlement():
    raw = {
        "event_ticker": "KXPRESCUP-26",
        "mutually_exclusive": True,
        "markets": [
            {
                "ticker": "KXPRESCUP-26-USA",
                "status": "active",
                "market_type": "binary",
                "rules_primary": "If Team USA wins the 2026 Presidents Cup, resolves Yes.",
                "rules_secondary": "A tie settles the Tie market.",
                "yes_bid_dollars": ".84",
                "yes_ask_dollars": ".86",
                "updated_time": (AT - timedelta(days=2)).isoformat(),
            }
        ],
    }
    quotes = golf_markets.normalize_cup_kalshi({"events": [raw]}, AT)
    assert quotes[0]["probability"] == pytest.approx(0.85)
    assert quotes[0]["observed_at"] == AT.isoformat()
    assert quotes[0]["source_updated_at"] != quotes[0]["observed_at"]
    wrong_year = {**raw, "event_ticker": "KXPRESCUP-28"}
    assert golf_markets.normalize_cup_kalshi({"events": [wrong_year]}, AT) == []
    raw["markets"][0]["yes_bid_dollars"] = ".99"
    assert golf_markets.normalize_cup_kalshi({"events": [raw]}, AT) == []


def test_polymarket_wide_spread_is_visible_with_settlement_mapping():
    raw = {
        "slug": "presidents-cup-2026",
        "active": True,
        "markets": [
            {
                "id": "1",
                "active": True,
                "outcomes": '["USA","International"]',
                "outcomePrices": '["0.5","0.5"]',
                "updatedAt": AT.isoformat(),
                "description": "2026 Presidents Cup. In a 15-15 tie, resolve 50-50.",
                "bestBid": 0.01,
                "bestAsk": 0.99,
            }
        ],
    }
    quotes = golf_markets.normalize_cup_polymarket([raw], AT)
    assert len(quotes) == 2
    assert all(q["contract_key"] == "winner-half-tie" for q in quotes)
    assert all(q["quality"] == "wide spread" for q in quotes)
    raw["markets"][0]["description"] = "Tie resolves to International"
    assert golf_markets.normalize_cup_polymarket([raw], AT) == []


def test_repeated_prices_and_return_to_prior_price_are_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "history.db")
    db.init_db()
    quotes = [
        quote(at=AT + timedelta(minutes=10 * i), probability=p)
        for i, p in enumerate([0.8, 0.8, 0.85, 0.8])
    ]
    assert golf_markets.store_quotes(quotes) == 4
    assert golf_markets.store_quotes(quotes) == 0
    assert [q["probability"] for q in golf_markets.quote_history(golf_cup.EVENT_ID)] == [
        0.8,
        0.8,
        0.85,
        0.8,
    ]
    assert len(golf_markets.quote_history(golf_cup.EVENT_ID, latest=True)) == 1
    event = {"id": "nfl:123"}
    readings = [
        sports_markets._reading(
            event=event,
            source="kalshi",
            source_event_id="a",
            market_id="b",
            away=p,
            home=1 - p,
            source_updated_at=AT.isoformat(),
            source_url="https://example.test",
            price_basis="test",
            at=AT + timedelta(minutes=i),
        )
        for i, p in enumerate([0.8, 0.85, 0.8])
    ]
    assert len({q["quote_hash"] for q in readings}) == 3


def test_model_rechecks_save_flat_history_before_start(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "model.db")
    db.init_db()
    raw = {
        "id": "123",
        "date": (AT + timedelta(hours=2)).isoformat(),
        "season": {"slug": "regular-season"},
        "status": {"type": {"state": "pre"}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": side,
                        "team": {"id": side, "displayName": side, "abbreviation": side},
                    }
                    for side in ["away", "home"]
                ]
            }
        ],
    }
    event = normalize_event("nfl", raw)
    for offset in [0, 0, 5, 10, 20]:
        store_events([copy.deepcopy(event)], observed_at=AT + timedelta(minutes=offset))
    saved = sports_event(event["id"])
    assert len(saved["prediction_history"]) == 3
    assert len({p["home_probability"] for p in saved["prediction_history"]}) == 1


def test_scorecard_compares_model_and_market_on_same_saved_games(tmp_path, monkeypatch):
    from runner_web.sports import _build_model_alpha

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "record.db")
    db.init_db()
    now = datetime.now(UTC)
    raw = {
        "id": "paired",
        "date": (now - timedelta(hours=1)).isoformat(),
        "season": {"slug": "regular-season"},
        "status": {"type": {"state": "pre"}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": side,
                        "team": {"id": side, "displayName": side, "abbreviation": side},
                    }
                    for side in ["away", "home"]
                ]
            }
        ],
    }
    event = normalize_event("nfl", raw)
    event.update(home_odds=-200, away_odds=180)
    store_events([event], observed_at=now - timedelta(hours=2))
    probability = sports_event(event["id"])["prediction"]
    event.update(status="post", completed=True, home_score=28, away_score=14)
    event["home"]["score"] = 28
    event["away"]["score"] = 14
    store_events([event], observed_at=now)
    record = _build_model_alpha("nfl")
    assert record["paired_games"] == 1
    assert record["paired_model_brier"] == round((probability["home_probability"] - 1) ** 2, 4)
    assert record["paired_market_brier"] == round(
        (probability["home_market_probability"] - 1) ** 2, 4
    )


def test_sentiment_is_selected_outcome_gap_even_when_favorite():
    event = team_event()
    favorite = ticker(event, now=AT)["indicator"]
    underdog = ticker(event, outcome="away", now=AT)["indicator"]
    assert favorite["sentiment"] == "negative"
    assert favorite["sentiment_mix"]["description"] == "GB: RATi 9.3 pp below Sportsbook"
    assert underdog["sentiment"] == "positive"
    assert underdog["sentiment_mix"]["description"] == "ATL: RATi 9.3 pp above Sportsbook"
    assert favorite["score"] is None
    assert favorite["risk"] == "detected"
    assert "bullish" not in favorite["description"]
    event["prediction"]["home_market_probability"] = 0.5914
    level = ticker(event, now=AT)["indicator"]
    assert level["sentiment"] == "neutral"
    assert level["sentiment_mix"]["state"] == "available"
    assert "in line with" in level["description"]


def test_attention_uses_capped_activity_and_keeps_venue_units_separate():
    event = cup_event()
    event["contract_quotes"] = [
        quote(at=AT - timedelta(minutes=20), probability=0.8),
        {**quote(probability=0.85), "volume_24h": 9999, "volume_unit": "contracts"},
        {**quote("polymarket", probability=0.85), "volume_24h": 9999, "volume_unit": "USD"},
    ]
    article = {
        "source_url": "https://example.test/news",
        "published_at": AT.isoformat(),
        "collected_at": AT.isoformat(),
    }
    event["news"] = [article, article]
    glyph = ticker(event, now=AT)["indicator"]
    assert glyph["score"] == 59  # 40 volume + 15 range + 4 unique story
    assert glyph["band"] == 2
    assert glyph["slices"][0]["value"] == pytest.approx(55)
    assert glyph["slices"][1]["value"] == 4
    assert "9,999 contracts" in glyph["attention_evidence"][0]
    assert any("9,999 USD" in text for text in glyph["attention_evidence"])
    assert len(glyph["attention_missing"]) == 0
    # Repeated polls and stories carry the same weight.
    event["contract_quotes"].extend([event["contract_quotes"][-2]] * 20)
    assert ticker(event, now=AT)["indicator"]["score"] == 59


def test_zero_attention_differs_from_missing_inputs():
    event = cup_event()
    assert ticker(event, now=AT)["indicator"]["mix_state"] == "unknown"
    event["contract_quotes"] = [
        quote(at=AT - timedelta(minutes=20)),
        {**quote(), "volume_24h": 0, "volume_unit": "contracts"},
    ]
    glyph = ticker(event, now=AT)["indicator"]
    assert glyph["score"] == 0
    assert glyph["mix_state"] == "zero"
    assert glyph["sentiment"] == "negative"  # direction remains independent


@pytest.mark.parametrize("invalid", [-1, "NaN", "Infinity", None, True])
def test_invalid_volume_stays_unknown(invalid):
    event = cup_event()
    event["contract_quotes"] = [{**quote(), "volume_24h": invalid, "volume_unit": "contracts"}]
    assert ticker(event, now=AT)["indicator"]["score"] is None


def test_attention_excludes_future_old_and_wide_price_points():
    event = cup_event()
    event["contract_quotes"] = [
        quote(at=AT - timedelta(days=2), probability=0.4),
        quote(at=AT - timedelta(minutes=20), probability=0.5, quality="wide spread"),
        quote(probability=0.8),
        quote(at=AT + timedelta(minutes=1), probability=0.9),
    ]
    glyph = ticker(event, now=AT)["indicator"]
    assert glyph["score"] is None
    assert glyph["sentiment"] == "positive"
    event["contract_quotes"].append(quote(at=AT - timedelta(minutes=10), probability=0.8))
    assert ticker(event, now=AT)["indicator"]["score"] == 0


def test_stale_or_wide_comparison_gets_unknown_sentiment_and_risk_marker():
    event = cup_event()
    event["contract_quotes"] = [quote(quality="wide spread")]
    glyph = ticker(event, now=AT)["indicator"]
    assert glyph["sentiment"] == "unknown"
    assert glyph["sentiment_mix"]["state"] == "unknown"
    assert glyph["risk"] == "significant"
    assert any("wide bid/ask" in s for s in glyph["risk_factors"])
    event["contract_quotes"] = [quote(at=AT - timedelta(hours=1))]
    glyph = ticker(event, now=AT)["indicator"]
    assert glyph["score"] is None
    assert any("price refresh" in s for s in glyph["risk_factors"])


def test_pregame_reading_freezes_at_kickoff_and_news_respects_collection_time():
    event = team_event()
    article = {
        "source_url": "https://example.test/news",
        "published_at": AT.isoformat(),
        "collected_at": (AT + timedelta(hours=1)).isoformat(),
    }
    event["news"] = [article]
    glyph = ticker(event, now=AT + timedelta(hours=2))["indicator"]
    assert glyph["scope"] == "Saved pregame reading"
    assert glyph["score"] is None
    assert glyph["sentiment"] == "negative"
    event["news"][0]["collected_at"] = AT.isoformat()
    assert ticker(event, now=AT + timedelta(hours=2))["indicator"]["score"] == 4


def test_settlement_contracts_keep_activity_and_sentiment_separate():
    event = cup_event()
    event["contract_quotes"] = [
        {**quote(probability=0.9), "volume_24h": 999, "volume_unit": "contracts"},
        quote("polymarket", probability=0.5, contract="winner-half-tie", quality="wide spread"),
    ]
    outright = ticker(event, now=AT)["indicator"]
    half_tie = ticker(event, contract="winner-half-tie", now=AT)["indicator"]
    assert outright["sentiment"] == "negative"
    assert outright["score"] == 30
    assert half_tie["sentiment"] == "unknown"
    assert half_tie["score"] is None
    assert half_tie["risk"] == "significant"


def test_unmodeled_golf_keeps_missing_values_clear():
    glyph = ticker({"id": "golf:other"}, now=AT)["indicator"]
    assert glyph["risk"] == "unknown"
    assert glyph["sentiment"] == "unknown"
    assert glyph["score"] is None
