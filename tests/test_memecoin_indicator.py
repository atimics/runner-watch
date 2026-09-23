from __future__ import annotations

import copy

import pytest

from runner_web.market_screens import detail, listing, row
from runner_web.stock_indicator import memecoin_indicator
from tests.test_market_screens import render, sample


def assessed_coin(**updates):
    return {
        **sample("memecoins"),
        "score": 58,
        "score_components": {"market": 28, "chain_event": 20, "social_search": 10},
        "chain_sentiment": "negative",
        "rug_score": 30,
        **updates,
    }


def test_quote_and_receipts_keep_attention_tone_and_risk_unknown():
    coin = {
        **sample("memecoins"),
        "change_24h": 1000,
        "sentiment": "positive",
        "runner_probability": 0.99,
        "findings": [{"kind": "liquidity_withdrawal", "severity": "high"}],
        "verified": True,
        "evidence_gate": {"state": "ready"},
        "eligibility": {"state": "eligible"},
    }
    display = row("memecoins", coin)
    glyph = display["indicator"]
    assert glyph["score"] is None
    assert glyph["mix_state"] == glyph["sentiment"] == glyph["risk"] == "unknown"
    assert glyph["gradient"] == ""
    assert "Attention unavailable" in glyph["description"]
    assert "verified" not in display
    assert "verification" not in glyph


@pytest.mark.parametrize("score,band", [(0, 1), (39.99, 1), (40, 2), (69.99, 2), (70, 3)])
def test_saved_token_attention_uses_shared_bands(score, band):
    assert memecoin_indicator(assessed_coin(score=score))["band"] == band


def test_chain_contribution_has_its_own_saved_basis():
    glyph = memecoin_indicator(
        assessed_coin(
            score_components={
                "market": 28,
                "chain_event": 20,
                "social_search": 10,
                "sec_event": 99,
                "news": 99,
                "rug": -90,
                "community": 99,
            }
        )
    )
    assert [part["value"] for part in glyph["slices"]] == [28, 20, 10]
    assert glyph["slices"][1]["label"] == "Chain evidence"
    assert glyph["slices"][1]["share"] == pytest.approx(20 / 58)
    assert glyph["sentiment"] == "negative"
    assert glyph["risk"] == "medium"


def test_saved_driver_fallback_and_known_zero():
    coin = assessed_coin(
        score_components=None,
        score_detail={
            "drivers": [{"key": "chain_event", "value": 58}],
            "penalties": [{"key": "rug", "value": -90}],
        },
    )
    glyph = memecoin_indicator(coin)
    assert [part["value"] for part in glyph["slices"]] == [0, 58, 0]
    assert memecoin_indicator(assessed_coin(score=0, score_components={"market": 0}))[
        "mix_state"
    ] == "zero"


def test_list_detail_and_source_are_consistent():
    coin = assessed_coin()
    original = copy.deepcopy(coin)
    board = listing("memecoins", [coin])
    display = detail("memecoins", {"coin": coin, "history": []})
    assert board["rows"][0]["indicator"] == display["item"]["indicator"]
    assert coin == original
    html = render(board)
    assert 'class="ticker-score indicator-glyph"' in html
    assert "Purple: chain evidence" in html
    assert "stock-indicator.css" in html
    assert "Verified evidence" not in html
