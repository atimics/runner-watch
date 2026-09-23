from __future__ import annotations

import copy
import math

import pytest

from runner_web.market_screens import detail, listing, row, state_tag
from runner_web.stock_indicator import stock_indicator


def stock(**overrides):
    return {
        "ticker": "GLYPH",
        "company": "Example",
        "score": 57.6,
        "price": 12.34,
        "change_pct": 8,
        "stage": "RUNNING",
        "trade_state": "TRIGGERED",
        "rug_level": "LOW",
        "rug_score": 10,
        "sentiment": "positive",
        "score_components": {
            "market": 40,
            "sec_event": 9.6,
            "news": 3,
            "social_search": 5,
            "community": 8,
        },
        "eligibility": {"state": "eligible"},
        "evidence_gate": {"state": "ready", "blockers": [], "checks": ["Market", "SEC", "News"]},
        "computed_at": "2026-09-21T19:00:00+00:00",
        **overrides,
    }


@pytest.mark.parametrize(
    ("score", "band"), [(0, 1), (39.99, 1), (40, 2), (69.99, 2), (70, 3), (100, 3)]
)
def test_attention_band_boundaries(score, band):
    assert stock_indicator(stock(score=score))["band"] == band


def test_three_angles_use_uncapped_post_freshness_contributions():
    glyph = stock_indicator(stock())
    assert [s["key"] for s in glyph["slices"]] == ["market", "evidence", "social"]
    assert [s["value"] for s in glyph["slices"]] == [40, 12.6, 5]
    assert sum(s["share"] for s in glyph["slices"]) == pytest.approx(1)
    assert glyph["slices"][0]["end"] == pytest.approx(40 / 57.6 * 100, abs=1e-6)
    capped = stock_indicator(
        stock(
            score=100,
            score_components={"market": 80, "sec_event": 12, "news": 6, "social_search": 8},
        )
    )
    assert capped["slices"][0]["share"] == pytest.approx(80 / 106)
    assert capped["slices"][-1]["end"] == 100


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), True, "not-a-score"])
def test_invalid_attention_is_not_a_fabricated_probability(bad):
    glyph = stock_indicator(stock(score=bad, score_components={}))
    assert glyph["score"] is None
    assert glyph["mix_state"] == "unknown"
    assert glyph["gradient"] == ""


def test_known_zero_and_missing_mix_are_different():
    assert stock_indicator(stock(score=0, score_components={"market": 0}))["mix_state"] == "zero"
    assert stock_indicator(stock(score=0, score_components={}))["mix_state"] == "unknown"
    glyph = stock_indicator(
        stock(score=50, score_components={"market": -5, "news": float("nan"), "community": 100})
    )
    assert glyph["gradient"] == ""
    assert all(s["value"] == 0 for s in glyph["slices"])


def test_driver_fallback_ignores_penalties_and_has_fixed_order():
    glyph = stock_indicator(
        stock(
            score_components=None,
            score_detail={
                "drivers": [
                    {"key": "social_search", "value": 5},
                    {"key": "market", "value": 10},
                    {"key": "news", "value": 15},
                ],
                "penalties": [{"key": "rug", "value": -90}],
            },
        )
    )
    assert [s["value"] for s in glyph["slices"]] == [10, 15, 5]
    assert "--red" not in glyph["gradient"]


@pytest.mark.parametrize(
    ("sentiment", "expected"),
    [
        ("positive", "positive"),
        ("risk", "negative"),
        ("negative", "negative"),
        ("neutral", "neutral"),
        ("mixed", "neutral"),
        ("gap", "unknown"),
        (None, "unknown"),
    ],
)
def test_sentiment_is_not_inferred_from_price_or_model(sentiment, expected):
    assert (
        stock_indicator(stock(sentiment=sentiment, change_pct=-50, runner_probability=0.99))[
            "sentiment"
        ]
        == expected
    )


@pytest.mark.parametrize(
    ("score", "level", "expected"),
    [
        (0, "LOW", "low"),
        (24.99, "LOW", "low"),
        (25, "GUARDED", "medium"),
        (49.99, "GUARDED", "medium"),
        (50, "HIGH", "high"),
        (75, "CRITICAL", "high"),
        (90, "LOW", "high"),
        (1, "HIGH", "high"),
        (None, "LOW", "low"),
        (None, "UNKNOWN", "unknown"),
        (math.nan, "LOW", "unknown"),
        (150, "LOW", "unknown"),
    ],
)
def test_risk_thresholds_and_conservative_conflicts(score, level, expected):
    assert stock_indicator(stock(rug_score=score, rug_level=level))["risk"] == expected


def test_hard_veto_dominates_low_risk_and_approval():
    glyph = stock_indicator(stock(hard_veto=True))
    assert glyph["risk"] == "high"
    assert not glyph["verification"]["verified"]


@pytest.mark.parametrize(
    "gate",
    [
        None,
        {},
        {"state": "gathering"},
        {"state": "near"},
        {"state": "blocked"},
        {"state": "ready", "blockers": ["Risk"]},
    ],
)
def test_running_or_eligible_alone_never_awards_a_check(gate):
    assert not row("stocks", stock(evidence_gate=gate, verified=True))["verified"]


@pytest.mark.parametrize("eligibility", [None, {}, {"state": "unknown"}, {"state": "blocked"}])
def test_unavailable_or_blocked_eligibility_never_awards_a_check(eligibility):
    assert not row("stocks", stock(eligibility=eligibility))["verified"]


def test_check_records_automated_basis_and_detail_uses_top_level_gate():
    display = row("stocks", stock())
    assert display["verified"]
    assert display["indicator"]["verification"]["basis"] == "automated_evidence_gate"
    payload = {
        "ticker": "GLYPH",
        "current": stock(evidence_gate=None),
        "evidence_gate": {"state": "ready", "blockers": []},
    }
    assert detail("stocks", payload)["item"]["verified"]


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"stage": "WATCH", "trade_state": "WATCH"}, "WATCH"),
        ({"stage": "RUNNING", "trade_state": "TRIGGERED"}, "RUNNING"),
        ({"stage": "EXTENDED"}, "EXTENDED"),
        ({"stage": "EARLY", "trade_state": "ARMED"}, "SETUP"),
        ({"trade_state": "AVOID"}, "AVOID"),
        ({"eligibility": {"state": "unknown"}}, "PAUSED"),
    ],
)
def test_status_filters_and_source_data_remain_unchanged(updates, expected):
    source = stock(**updates)
    saved = copy.deepcopy(source)
    tag, tone, risk = state_tag(source)
    for gate in [{"state": "ready"}, {"state": "gathering"}]:
        result = row("stocks", {**source, "evidence_gate": gate})
        assert (result["tag"], result["tag_tone"], result["risk"]) == (tag, tone, risk)
        assert result["tag"] == expected
    assert source == saved
    assert listing("stocks", [source])["counts"] == {tone: 1}


def test_sports_keeps_its_own_display():
    display = row(
        "sports",
        {"id": "test", "symbol": "TEST", "verified": True, "start_time": "2026-09-21T18:00:00Z"},
    )
    assert "indicator" not in display
    assert "verified" not in display


@pytest.mark.parametrize("gate", [None, {}])
def test_empty_authoritative_detail_gate_does_not_resurrect_saved_verification(gate):
    payload = {"ticker": "GLYPH", "current": stock(), "evidence_gate": gate}
    assert not detail("stocks", payload)["item"]["verified"]


def test_shared_base_without_market_screen_does_not_load_stock_glyph():
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("web/templates"), autoescape=True)
    html = env.from_string(
        '{% extends "market_screen.html" %}'
        "{% block screen_title %}Report{% endblock %}"
        "{% block topbar %}{% endblock %}"
        "{% block screen_main %}<p>Saved report</p>{% endblock %}"
    ).render(static_version="test")
    assert "Saved report" in html
    assert "stock-indicator.js" not in html
    assert "stock-indicator.css" not in html
    assert "Indicator key" not in html


@pytest.mark.parametrize("bullish,bearish", [(3, 1), (0, 4), (4, 0), (1, 1), (1, 7)])
def test_sentiment_counts_set_directional_shares_independent_of_attention_and_risk(
    bullish, bearish
):
    source = stock(sentiment_counts={"bullish": bullish, "bearish": bearish}, rug_score=75)
    original = copy.deepcopy(source)
    glyph = stock_indicator(source)
    mix = glyph["sentiment_mix"]
    assert mix["state"] == "available"
    assert mix["bullish"] == pytest.approx(bullish / (bullish + bearish))
    assert mix["bullish"] + mix["bearish"] == 1
    assert "var(--green)" in mix["gradient"] and "var(--red)" in mix["gradient"]
    assert mix["description"] in glyph["description"]
    assert glyph["risk"] == "high"
    assert source == original
    assert (
        listing("stocks", [source])["rows"][0]["indicator"]
        == detail(
            "stocks",
            {"ticker": "GLYPH", "current": source, "evidence_gate": source["evidence_gate"]},
        )["item"]["indicator"]
    )


@pytest.mark.parametrize(
    "counts",
    [
        {},
        [],
        {"bullish": 3},
        {"bullish": 0, "bearish": 0},
        {"bullish": -1, "bearish": 2},
        {"bullish": True, "bearish": 1},
        {"bullish": math.nan, "bearish": 1},
        {"bullish": math.inf, "bearish": 1},
        {"bullish": 1e308, "bearish": 1e308},
    ],
)
def test_unusable_counts_keep_a_gap_even_with_a_positive_label(counts):
    mix = stock_indicator(stock(sentiment_counts=counts))["sentiment_mix"]
    assert mix["state"] == "unknown"
    assert mix["bullish"] is mix["bearish"] is None
    assert mix["gradient"] == ""
    assert "split unavailable" in mix["description"]


@pytest.mark.parametrize(
    "tone,bullish",
    [
        ("positive", 1),
        ("bullish", 1),
        ("risk", 0),
        ("negative", 0),
        ("bearish", 0),
        ("neutral", None),
        ("mixed", None),
        (None, None),
        ("gap", None),
    ],
)
def test_single_saved_direction_and_neutral_only_gap(tone, bullish):
    mix = stock_indicator(stock(sentiment=tone))["sentiment_mix"]
    assert mix["bullish"] == bullish
    assert mix["state"] == ("unknown" if bullish is None else "available")
