"""Attention and eligibility: two questions, two answers.

The acceptance case from the scoring review: a name with an active halt, a risk
filing and a veto-level risk score must be able to sit at the top of the list
*and* be unambiguously blocked. Before the split, the deductions that blocked it
also pushed it down the list.
"""

from __future__ import annotations

import pytest

from runner_web import attention
from runner_web import main as web_main


def test_attention_reads_evidence_not_direction():
    positive = attention.attention_score(signal=40, event=attention.event_attention(80))
    negative = attention.attention_score(signal=40, event=attention.event_attention(80))
    assert positive == negative == 49.6


def test_attention_is_clamped_to_the_list_window():
    assert attention.attention_score(signal=95, event=12, news=6, social=8, community=8) == 100.0
    assert attention.attention_score(signal=0) == 0.0


def test_only_active_callers_order_the_list():
    assert attention.community_attention(0) == 0.0
    assert attention.community_attention(3) == 4.0
    # Eight comments used to reach the cap on their own; comments are logged now,
    # so no number of them moves this.
    assert attention.community_attention(0) == 0.0
    assert attention.community_attention(7) == 6.0


def test_eligibility_is_deterministic_policy():
    assert attention.eligibility()["state"] == "eligible"
    assert attention.eligibility()["blocked"] is False

    halted = attention.eligibility(active_halt=True, trade_state="AVOID", rug_score=90)
    assert halted["state"] == "blocked"
    assert [reason["code"] for reason in halted["reasons"]] == [
        "trading_halt",
        "state_avoid",
        "rug_score",
    ]
    assert attention.risk_note(halted) == (
        "Trading halt · Scanner state AVOID · Reported risk score at veto level"
    )

    unknown = attention.eligibility(has_price=False)
    assert unknown["state"] == "unknown"
    assert unknown["blocked"] is False


def test_an_exit_state_blocks_without_a_halt():
    blocked = attention.eligibility(trade_state="exit")
    assert blocked["state"] == "blocked"
    assert blocked["reasons"][0]["code"] == "state_exit"


def test_a_critical_rug_level_blocks():
    blocked = attention.eligibility(rug_level="critical", rug_score=10)
    assert blocked["state"] == "blocked"
    assert blocked["reasons"][0]["code"] == "rug_risk"


@pytest.mark.parametrize("state", ["EXIT", "AVOID"])
def test_a_blocked_name_can_still_lead_attention(state):
    snapshot = {
        "id": "scan",
        "ticker": "RISK",
        "score": 88,
        "price": 4.2,
        "rug_score": 90,
        "rug_level": "CRITICAL",
        "trade_state": state,
    }
    inputs = {
        "score_as_of": "2026-09-21T14:00:00+00:00",
        "predictions": {},
        "filings_by_ticker": {"RISK": {"form": "S-3", "score": 90, "sentiment": "risk"}},
        "community": {"RISK": {"call_count": 0, "comment_count": 40}},
        "market_events_by_ticker": {
            "RISK": [
                {
                    "event_type": "trading_halt",
                    "status": "active",
                    "event_at": "2026-09-21T13:00:00+00:00",
                }
            ]
        },
    }

    scored = web_main._pulse_snapshot_score(snapshot, inputs)

    # The material event and the risk filing raise attention...
    assert scored["score"] >= 88
    assert scored["score_components"]["sec_event"] > 0
    # ...while the block is stated plainly, with reasons.
    assert scored["eligibility"]["state"] == "blocked"
    assert scored["eligibility"]["blocked"] is True
    assert "Trading halt" in scored["eligibility_note"]
    # The deductions are reported beside the score, not taken out of it.
    assert scored["policy_components"]["rug"] == -27.0
    assert "rug" not in scored["score_components"]


def test_comments_no_longer_move_attention():
    snapshot = {"id": "scan", "ticker": "QUIET", "score": 50, "price": 10.0}
    base = {
        "score_as_of": "2026-09-21T14:00:00+00:00",
        "predictions": {},
        "filings_by_ticker": {},
        "market_events_by_ticker": {},
    }
    with_comments = web_main._pulse_snapshot_score(
        snapshot, {**base, "community": {"QUIET": {"call_count": 0, "comment_count": 25}}}
    )
    with_callers = web_main._pulse_snapshot_score(
        snapshot, {**base, "community": {"QUIET": {"call_count": 3, "comment_count": 0}}}
    )

    assert with_comments["score"] == 50.0
    assert with_comments["comment_count"] == 25
    assert with_callers["score"] == 54.0