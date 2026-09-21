"""Contracts that prevent attention, probabilities and policy from being conflated."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from runner_web.attention import (
    attention_order,
    attention_score,
    community_attention,
    eligibility,
    event_attention,
    forecast_facts,
    market_activity,
)
from runner_web.replay import evaluate_served_policy, purged_chronological_split


def activity(**changes):
    return {
        "relative_volume": 4,
        "recent_relative_volume": 8,
        "momentum_5m_pct": 2,
        "momentum_15m_pct": 6,
        "change_pct": 10,
        "stale_minutes": 0,
        **changes,
    }


def groups(count=320, minutes=5):
    start = datetime(2026, 9, 1, 14, tzinfo=UTC)
    return [
        [
            {
                "scan_run_id": f"r{i}",
                "run_captured_at": (start + timedelta(minutes=i * minutes)).isoformat(),
                "ticker": "ONE",
            }
        ]
        for i in range(count)
    ]


def test_activity_is_direction_neutral_and_does_not_read_forecast_or_legacy_score():
    positive = market_activity(activity(score=90, probability_up=0.9))
    negative = market_activity(
        activity(
            score=1, probability_up=0.01, momentum_5m_pct=-2, momentum_15m_pct=-6, change_pct=-10
        )
    )
    assert positive == negative
    assert positive["value"] == 46
    assert positive["status"] == "available"


@pytest.mark.parametrize("age", [None, -1, float("nan"), float("inf")])
def test_unknown_and_future_clocks_never_look_fresh(age):
    result = market_activity(activity(stale_minutes=age))
    assert result["value"] == 0
    assert result["status"] == "unavailable"


def test_market_activity_ages_without_new_evidence():
    assert (
        market_activity(activity(stale_minutes=30))["value"] < market_activity(activity())["value"]
    )
    assert market_activity({"score": 95, "stale_minutes": 0})["status"] == "unavailable"


def test_boosts_cannot_be_infinite_negative_or_self_generated():
    assert community_attention(10**100) == 0
    assert attention_score(signal=40, community=100) == 40
    assert attention_score(signal=float("nan"), news=float("inf")) == 0
    assert event_attention(float("nan")) == 0
    assert attention_score(signal=40, event=999, news=999, social=999) == 66


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"has_price": False}, "no_price"),
        ({"stale_minutes": None}, "quote_age_unknown"),
        ({"stale_minutes": 16}, "stale_quote"),
        ({"stale_minutes": -1}, "stale_quote"),
        ({"trade_state": "UNKNOWN"}, "risk_unassessed"),
        ({"trade_state": "invented"}, "risk_unassessed"),
        ({"rug_score": None}, "risk_unknown"),
        ({"rug_score": -1}, "risk_unknown"),
    ],
)
def test_eligibility_requires_complete_evidence(overrides, code):
    args = {
        "trade_state": "WATCH",
        "rug_score": 0,
        "has_price": True,
        "stale_minutes": 1,
        "require_complete": True,
        **overrides,
    }
    result = eligibility(**args)
    assert result["state"] == "unknown"
    assert result["eligible"] is False
    assert code in {reason["code"] for reason in result["reasons"]}


def test_veto_dominates_unknowns_without_reducing_attention():
    result = eligibility(hard_veto=True, has_price=False, require_complete=True)
    assert result["blocked"] is True
    assert eligibility(rug_score=50)["blocked"] is True
    assert (
        eligibility(
            trade_state="ARMED", rug_score=0, has_price=True, stale_minutes=0, require_complete=True
        )["eligible"]
        is True
    )
    rows = [
        {"ticker": "UP", "score": 99},
        {"ticker": "HALT", "score": 1, "attention_urgent": True, "eligibility": result},
    ]
    assert sorted(rows, key=attention_order)[0]["ticker"] == "HALT"


def test_forecast_retains_units_and_names_its_assumptions():
    result = forecast_facts(
        {
            "probability_up": 0.4,
            "probability_down": 0.1,
            "probability_timeout": 0.5,
            "expected_return_pct": 2.8,
        }
    )
    assert result["activity_probability"] == 0.5
    assert result["assumed_barrier_payoff_pct"] == 2.8
    assert result["probability_up"] == 0.4
    assert result["contract_basis"] == "legacy_assumed_v1"
    assert "not total drawdown" in result["downside_note"]


@pytest.mark.parametrize(
    "override",
    [
        {"probability_up": 2},
        {"probability_down": float("nan")},
        {"probability_timeout": None},
        {"probability_up": 0.41},
        {"label_contract": "{}"},
        {"label_contract": "bad-json"},
    ],
)
def test_invalid_forecasts_never_become_confident_numbers(override):
    assert (
        forecast_facts(
            {"probability_up": 0.4, "probability_down": 0.1, "probability_timeout": 0.5, **override}
        )
        is None
    )


def test_split_reads_real_training_timestamps_and_freezes_both_boundaries():
    original = groups()
    result = purged_chronological_split(original)
    # Original 256/32/32 memberships; remove 14 overlapping labels at each boundary.
    assert [len(result[key]) for key in ("train", "validation", "test")] == [242, 18, 32]
    assert result["receipt"]["purged_groups"] == 28
    assert result["validation"][0] == original[256]
    assert result["test"] == original[288:]
    # Concatenating and recomputing 80/10/10 would move validation rows into training.
    kept = sum(len(result[key]) for key in ("train", "validation", "test"))
    assert int(0.8 * kept) != len(result["train"])
    for left, right in (("train", "validation"), ("validation", "test")):
        end = datetime.fromisoformat(result[left][-1][0]["run_captured_at"]) + timedelta(minutes=70)
        assert end < datetime.fromisoformat(result[right][0][0]["run_captured_at"])


def test_invalid_timestamps_cannot_silently_disable_purging():
    rows = groups(count=20)
    rows[3][0]["run_captured_at"] = "not-a-time"
    with pytest.raises(ValueError, match="timestamp"):
        purged_chronological_split(rows)
    with pytest.raises(ValueError, match="chronologically"):
        purged_chronological_split(list(reversed(groups(count=20))))


def test_every_row_label_end_counts_and_equal_boundary_is_purged():
    rows = groups(count=20, minutes=120)
    rows[0].append(
        {
            "scan_run_id": "r0",
            "run_captured_at": rows[0][0]["run_captured_at"],
            "label_end_at": rows[16][0]["run_captured_at"],
        }
    )
    result = purged_chronological_split(rows)
    assert "r0" in result["receipt"]["purged_runs"]
    assert result["test"] == rows[18:]


def test_replay_does_not_replace_unresolved_top_rows_or_count_ambiguity_as_fact():
    rows = [
        {"ticker": "MISSING", "attention": 99, "barrier_label": None},
        {
            "ticker": "AMBIGUOUS",
            "attention": 98,
            "barrier_label": "down",
            "barrier_resolution": "ambiguous",
        },
        {"ticker": "KNOWN", "attention": 97, "barrier_label": "up"},
    ]
    report = evaluate_served_policy(rows, k=2)
    assert report["k"] == 2
    assert report["selected_unresolved"] == 2
    assert report["served_precision"] is None
    assert report["coverage_at_k"] == 0
    assert report["precision_lower_bound"] == 0
    assert report["precision_upper_bound"] == 1


def test_replay_uses_the_same_urgency_and_tie_breaking_as_the_list():
    rows = [
        {"ticker": "B", "attention": 99, "barrier_label": "timeout"},
        {
            "ticker": "HALT",
            "attention": 1,
            "attention_urgent": True,
            "barrier_label": "down",
            "eligibility": {"blocked": True},
        },
    ]
    report = evaluate_served_policy(rows, k=1)
    assert report["served_precision"] == 1
    assert report["blocked_in_top"] == 1
    with pytest.raises(ValueError, match="each scan"):
        evaluate_served_policy([{**rows[0], "scan_run_id": "1"}, {**rows[1], "scan_run_id": "2"}])
    with pytest.raises(ValueError, match="positive integer"):
        evaluate_served_policy(rows, k=0)
