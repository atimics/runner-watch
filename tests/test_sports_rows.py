from __future__ import annotations

import pytest

from runner_web.market_screens import row


def game(**extra):
    return {
        "id": "mlb:row-test",
        "league": "mlb",
        "away_abbreviation": "ARI",
        "home_abbreviation": "COL",
        "away_team_name": "Arizona Diamondbacks",
        "home_team_name": "Colorado Rockies",
        "status": "pre",
        "start_time": "2099-09-24T02:00:00Z",
        "prediction": {
            "selection": "home",
            "signal": "watch",
            "home_probability": 0.44,
            "away_probability": 0.56,
            "observed_at": "2026-09-24T00:00:00Z",
        },
        **extra,
    }


def test_pregame_names_favorite_in_forecast_while_teams_keep_equal_weight():
    result = row("sports", game())
    assert result["selected_team_label"] == "COL"
    assert result["tag"] == "WATCH"
    assert result["matchup"]["emphasis"] is None
    assert result["matchup"]["emphasis_label"] == ""
    assert result["matchup"]["forecast"]["label"] == "ARI 56%"
    assert result["value"] == "vs"


@pytest.mark.parametrize("status,label", [("in", "Leading"), ("post", "Winner")])
@pytest.mark.parametrize("scores,side", [((5, 3), "away"), ((2, 5), "home")])
def test_actual_scores_choose_emphasis_and_keep_score_order(status, label, scores, side):
    result = row("sports", game(status=status, away_score=scores[0], home_score=scores[1]))
    matchup = result["matchup"]
    assert matchup["emphasis"] == side
    assert matchup["emphasis_label"] == label
    assert [t["side"] for t in matchup["teams"]] == ["away", "home"]
    assert [t["side"] for t in matchup["teams"] if t["emphasized"]] == [side]
    assert result["value"] == f"{scores[0]} – {scores[1]}"
    assert matchup["forecast"]["label"] == "ARI 56%"
    assert "Pregame model" in matchup["forecast"]["description"]


@pytest.mark.parametrize("status", ["in", "post"])
@pytest.mark.parametrize("scores", [(0, 0), (3, 3), (None, 5), (5, None)])
def test_ties_and_missing_scores_keep_both_teams_equal(status, scores):
    result = row("sports", game(status=status, away_score=scores[0], home_score=scores[1]))
    assert result["matchup"]["emphasis"] is None
    assert not any(team["emphasized"] for team in result["matchup"]["teams"])
    assert result["matchup"]["tied"] == (scores[0] == scores[1])


def test_late_pregame_feed_waits_for_scores_before_highlighting_a_leader():
    result = row("sports", game(start_time="2020-01-01T00:00:00Z"))
    assert result["change"] == "Score pending"
    assert result["matchup"]["emphasis"] is None


@pytest.mark.parametrize(
    "prediction",
    [
        {},
        {"home_probability": True},
        {"home_probability": True, "away_probability": 0.6},
        {"home_probability": 0.4, "away_probability": "invalid"},
        {"home_probability": float("nan")},
        {"home_probability": 1.1},
        {"home_probability": -0.1},
        {"home_probability": 0.7, "away_probability": 0.7},
    ],
)
def test_unavailable_or_invalid_forecast_leaves_the_ring_out(prediction):
    result = row("sports", game(prediction=prediction))
    assert result["matchup"]["forecast"] is None
    assert result["matchup"]["emphasis"] is None


def test_even_model_keeps_equal_teams_and_names_the_even_chance():
    result = row("sports", game(prediction={"home_probability": 0.5}))
    assert result["matchup"]["emphasis"] is None
    assert result["matchup"]["forecast"]["label"] == "Even 50–50"
    assert result["matchup"]["forecast"]["team"] == "Even"


def test_forecast_uses_probability_even_when_a_separate_runner_score_exists():
    result = row("sports", game(score=82))
    assert result["score"] == 82
    assert result["matchup"]["forecast"]["percent"] == 56


def test_saved_factors_follow_the_named_favorite_and_keep_market_separate():
    prediction = {
        "model_version": "team-form-v1",
        "home_probability": 0.433193,
        "away_probability": 0.566807,
        "home_market_probability": 0.489166,
        "away_market_probability": 0.510834,
        "factors": {
            "baseline_pct": 50,
            "home_record_delta_pp": -10.180723,
            "home_venue_delta_pp": 3.5,
            "home_clamp_delta_pp": 0,
            "home_probability_pct": 43.319277,
            "away_record": {"wins": 90, "losses": 60},
            "home_record": {"wins": 64, "losses": 86},
        },
    }
    forecast = row("sports", game(prediction=prediction))["matchup"]["forecast"]
    assert forecast["team"] == "ARI"
    assert [round(part["value"], 1) for part in forecast["factors"]] == [10.2, -3.5, 0]
    assert sum(part["share"] for part in forecast["factors"]) == pytest.approx(100)
    assert forecast["market_percent"] == 51.1
    assert forecast["market_gap_pp"] == 5.6
    assert "Venue -3.5 points" in forecast["description"]
