from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest

from runner_web import memecoins
from runner_web.memecoin_forensics import analyze_events
from runner_web.memecoin_model import VERSION, assess_memecoin, display_assessment
from runner_web.stock_indicator import memecoin_indicator
from tests.test_chain_discovery import market_db, pool  # noqa: F401
from tests.test_market_screens import render
from tests.test_memecoin_forensics import AT, event, swap


def quote(**changes):
    return {
        "id": "chain-test",
        "symbol": "TEST",
        "name": "Test",
        "network": "solana",
        "token_address": "mint",
        "price": 0.01,
        "volume_24h": 100_000,
        "market_cap": None,
        "liquidity_usd": 10_000,
        "change_24h": 20,
        "observed_at": AT.isoformat(),
        "source_url": "https://example.test/pool",
        **changes,
    }


def assess(events=(), *, row=None, coverage=None, findings=None):
    events = list(events)
    return assess_memecoin(
        row or quote(),
        events=events,
        findings=analyze_events(events)["findings"] if findings is None else findings,
        coverage={"checked_at": AT.isoformat(), "partial": True} if coverage is None else coverage,
        at=AT,
    )


def test_partial_coverage_preserves_unknown_checks_with_observed_activity():
    model = assess([swap("buy", "buyer", second=-20)])
    receipt = model["memecoin_assessment"]
    assert receipt["version"] == VERSION
    assert receipt["state"] == receipt["coverage"]["state"] == "partial"
    assert receipt["risk"]["state"] == "unknown"
    assert "Sale proceeds at a stated position size" in receipt["risk"]["missing_checks"]
    assert model["attention_score"] > 0
    assert "rug_score" not in model and "runner_probability" not in model
    assert memecoin_indicator(model)["risk"] == "unknown"


def test_direction_is_net_sampled_wallet_balance_with_receipts():
    events = [
        swap("a1", "a", amount="20", second=-80),
        swap("a2", "a", "sell", amount="5", second=-60),
        swap("b1", "b", "sell", amount="30", second=-40),
        swap("c1", "c", amount="40", second=-20),
        swap("c2", "c", "sell", amount="40", second=-10),
    ]
    model = assess(events)
    flow = model["memecoin_assessment"]["sentiment"]
    assert (flow["net_buyers"], flow["net_sellers"], flow["balanced_wallets"]) == (1, 1, 1)
    assert len(flow["evidence"]) == 5
    glyph = memecoin_indicator(model)
    assert glyph["sentiment_mix"]["bullish"] == 0.5
    assert "50% net buying wallets, 50% net selling wallets" in glyph["description"]
    assert "15-minute swap sample" in glyph["description"]


def test_instruction_duplicates_and_repeated_buys_share_one_wallet_reading():
    first = swap("a1", "a", second=-50)
    result = assess([first, {**first, "event_id": "inner"}, swap("a2", "a", second=-20)])
    flow = result["memecoin_assessment"]["sentiment"]
    assert flow["observed_swaps"] == 2
    assert flow["net_buyers"] == 1
    assert result["attention_score"] == assess([first])["attention_score"]


def test_launch_wallets_and_round_trips_are_excluded_from_direction():
    events = [
        event("token_launch", "launch", second=-500, wallet="creator", token_address="mint"),
        swap("creator-buy", "creator", second=-400),
        *[
            swap(str(i), "round-trip", "buy" if i % 2 == 0 else "sell", second=-200 + i * 20)
            for i in range(4)
        ],
    ]
    model = assess(events)
    assert model["chain_sentiment"] is None
    assert model["chain_sentiment_counts"] is None
    flow = model["memecoin_assessment"]["sentiment"]
    assert {item["wallet"] for item in flow["excluded_wallets"]} == {"creator", "round-trip"}
    assert model["memecoin_assessment"]["risk"]["factors"][0]["basis"] == "pattern"


def test_shared_funding_is_a_pattern_filter_with_original_caveat():
    events = [swap("a", "a", amount="10", second=-100), swap("b", "b", amount="20", second=-90)]
    events += [
        event(
            "sol_transfer",
            "fund-" + wallet,
            second=-200,
            source_wallet="root",
            wallet=wallet,
            lamports="1000",
        )
        for wallet in ("a", "b")
    ]
    result = assess(events)
    assert result["chain_sentiment_counts"] is None
    risk = result["memecoin_assessment"]["risk"]["factors"][0]
    assert risk["basis"] == "relationship"
    assert "Shared services" in risk["explanation"]


def test_risk_and_large_negative_move_can_both_raise_attention():
    base = assess(row=quote(change_24h=80, volume_24h=1e9))
    withdrawal = event(
        "liquidity_withdrawal",
        "withdraw",
        second=-5,
        wallet="lp",
        token_address="mint",
        net_token_amount="100",
    )
    risky = assess([withdrawal], row=quote(change_24h=-80, volume_24h=1e9))
    assert risky["attention_score"] > base["attention_score"]
    assert risky["attention_score"] >= 70
    assert memecoin_indicator(risky)["risk"] == "detected"
    assert risky["chain_sentiment"] is None
    assert risky["attention_score"] == sum(
        value or 0 for value in risky["score_components"].values()
    )
    assert risky["score_components"]["market"] == base["score_components"]["market"]


def test_cap_liquidity_social_and_promotion_claims_leave_assessment_unchanged():
    events = [swap("buy", "wallet", second=-1)]
    original = assess(events)
    altered = assess(
        events,
        row=quote(
            market_cap=1e12, liquidity_usd=1e12, sentiment="positive", social_search=100, boosts=500
        ),
    )
    assert altered == original


@pytest.mark.parametrize(
    "coverage",
    [
        {},
        {"checked_at": (AT - timedelta(minutes=16)).isoformat()},
        {"checked_at": (AT + timedelta(seconds=1)).isoformat()},
    ],
)
def test_chain_freshness_is_separate_from_quote_freshness(coverage):
    result = assess([swap("buy", "a", second=-5)], coverage=coverage)
    assert result["attention_score"] == assess()["attention_score"]
    assert result["chain_sentiment_counts"] is None
    assert result["score_components"]["chain_event"] is None


def test_gap_counts_and_empty_samples_keep_risk_unknown():
    result = assess(coverage={"checked_at": AT.isoformat(), "recorded_coverage_gaps": 8})
    assert result["memecoin_assessment"]["coverage"]["recorded_coverage_gaps"] == 8
    assert memecoin_indicator(result)["risk"] == "unknown"


def test_other_tokens_future_events_and_old_swaps_leave_direction_unknown():
    events = [
        swap("future", "a", second=1),
        swap("old", "b", second=-901),
        {**swap("other", "c", second=-10), "token_address": "other-mint"},
    ]
    result = assess(events)
    assert result["chain_sentiment_counts"] is None
    assert result["score_components"]["chain_event"] is None


def test_risk_window_and_receipts_bound_the_factors():
    events = [
        event(
            "liquidity_withdrawal",
            "old",
            second=-86401,
            wallet="lp",
            token_address="mint",
            net_token_amount="100",
        )
    ]
    fake = {
        "id": "unsupported",
        "token_address": "mint",
        "kind": "creator_sell",
        "title": "Claim",
        "observed_at": AT.isoformat(),
        "evidence": [],
    }
    result = assess(events, findings=[fake, *analyze_events(events)["findings"]])
    assert result["risks"] == []
    assert result["memecoin_assessment"]["risk"]["state"] == "unknown"


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "0", "bad", None])
def test_invalid_swap_amounts_leave_direction_unknown(amount):
    result = assess([swap("bad", "a", amount=amount)], findings=[])
    assert result["chain_sentiment_counts"] is None


def test_stale_display_preserves_receipt_and_expires_current_readings():
    original = {**quote(), **assess([swap("buy", "a", second=-1)])}
    before = copy.deepcopy(original)
    displayed = display_assessment(original, at=AT + timedelta(minutes=16))
    assert original == before
    assert displayed["attention_score"] is None
    assert displayed["chain_sentiment_counts"] is None
    assert displayed["memecoin_assessment"]["attention"]["value"] > 0
    assert displayed["memecoin_assessment"]["state"] == "stale"
    assert memecoin_indicator(displayed)["score"] is None


def test_creator_finding_retains_exclusion_when_launch_leaves_event_window():
    launch = event("token_launch", "launch", second=-2000, wallet="creator", token_address="mint")
    trade = swap("creator-buy", "creator", second=-10)
    result = assess([trade], findings=analyze_events([launch, trade])["findings"])
    assert result["chain_sentiment_counts"] is None
    assert result["memecoin_assessment"]["sentiment"]["excluded_wallets"] == [
        {"wallet": "creator", "reasons": ["launch_link"]}
    ]


def test_current_risk_flags_expire_while_historical_receipt_is_preserved():
    withdrawal = event(
        "liquidity_withdrawal",
        "withdraw",
        second=-30,
        wallet="lp",
        token_address="mint",
        net_token_amount="100",
    )
    result = {**quote(), **assess([withdrawal])}
    displayed = display_assessment(result, at=AT + timedelta(hours=25))
    assert displayed["risks"] == []
    assert memecoin_indicator(displayed)["risk"] == "unknown"
    assert displayed["memecoin_assessment"]["risk"]["factors"]
    assert result["risks"]


def test_solana_receipts_are_isolated_from_other_networks():
    result = assess([swap("buy", "a", second=-1)], row=quote(network="base"))
    assert result["chain_sentiment_counts"] is None
    assert result["score_components"]["chain_event"] is None


def test_worker_saves_same_model_for_list_detail_and_render(market_db, monkeypatch):  # noqa: F811
    from runner_web.market_screens import detail, listing
    from runner_web.memecoin_store import stored_memecoin

    events = [{**swap("buy", "a", second=-10), "token_address": "AbC123"}]
    monkeypatch.setattr(
        memecoins,
        "discover_pools",
        lambda **_: {
            "pools": [
                {
                    "pool_address": "Pool123",
                    "token_address": "AbC123",
                    "created_at": (AT - timedelta(minutes=10)).isoformat(),
                }
            ],
            "events": events,
            "partial": True,
            "coverage": {"recorded_coverage_gaps": 2},
        },
    )
    assert (
        memecoins.refresh_memecoins(
            at=AT, download=lambda *_: json.dumps({"data": [pool()]}).encode()
        )["status"]
        == "ok"
    )
    board = memecoins.memecoin_market(at=AT)
    coin = board["rows"][0]
    opened = memecoins.memecoin_detail(coin["id"], at=AT)
    saved = stored_memecoin(coin["id"])["coin"]
    assert saved["memecoin_assessment"] == opened["coin"]["memecoin_assessment"]
    assert saved["memecoin_assessment"] == coin["memecoin_assessment"]
    assert (
        listing("memecoins", [coin])["rows"][0]["indicator"]
        == detail("memecoins", opened)["item"]["indicator"]
    )
    html = render(detail("memecoins", opened))
    assert "Attention" in html and "15-minute swap sample" in html
    assert "Awaiting data: Sale proceeds at a stated position size" in html
    assert "2" in str(saved["memecoin_assessment"]["coverage"]["recorded_coverage_gaps"])
    paused = memecoins.memecoin_detail(coin["id"], at=AT + timedelta(minutes=16))
    assert paused["coin"]["attention_score"] is None
    assert "Refresh the quote and chain evidence" in render(detail("memecoins", paused))
