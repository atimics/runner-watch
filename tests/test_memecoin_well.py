"""The Well: a memecoin's sigil, drawn around its liquidity."""

from __future__ import annotations

import pytest

from runner_web.memecoin_well import (
    flow_state,
    gauge_arcs,
    liquidity_state,
    radius,
    standards_state,
    well_key,
    well_svg,
)
from runner_web.ring_glyph_image import glyph_image
from runner_web.stock_indicator import memecoin_indicator


def coin(**extra):
    return {
        "venue": "pool",
        "real_liquidity_usd": 64_843.0,
        "liquidity_usd": 67_824.0,
        "fully_diluted_valuation": 506_209.0,
        "liquidity_lock": {"left_pct": 0.0, "dex": "PumpSwap"},
        **extra,
    }


def test_one_log_scale_from_the_centre_out():
    assert radius(100) == 4 and radius(10_000_000) == 64
    assert radius(1_000) < radius(10_000) < radius(100_000)
    assert radius(None) == radius(0) == 0
    assert radius(10**12) == 64  # the chamber never leaves the frame


@pytest.mark.parametrize(
    ("lock", "state"),
    [
        ({"left_pct": 0.0, "dex": "PumpSwap"}, "sealed"),
        # The wall seals where the RATi Rules' lock standard is met: 10% or less held.
        ({"left_pct": 10.0, "dex": "Raydium CPMM"}, "sealed"),
        ({"left_pct": 40.0, "dex": "Raydium CPMM"}, "open"),
        ({"left_pct": None, "dex": None}, "unread"),
        ({"left_pct": None, "dex": "Raydium AMM v4"}, "unread"),
        (None, "unchecked"),
    ],
)
def test_the_wall_follows_the_liquidity_lock(lock, state):
    assert liquidity_state(coin(liquidity_lock=lock))["lock"] == state


def test_a_phantom_pool_shows_the_liquidity_it_does_not_hold():
    # Live shape: quoted $2,174 with $72.88 of real liquidity in the pool.
    well = liquidity_state(coin(real_liquidity_usd=72.88, liquidity_usd=2_174.87))
    assert well["phantom"] is True
    assert well["radii"]["quoted"] > well["radii"]["real"]
    assert "the pool quotes $2.2K, 30×" in well["reading"]
    assert 'class="well-phantom"' in well_svg({"band": 1, "liquidity": well})
    assert liquidity_state(coin())["phantom"] is False


def test_unknown_real_liquidity_is_a_question_never_an_empty_pool():
    well = liquidity_state(coin(real_liquidity_usd=None))
    svg = well_svg({"band": 1, "liquidity": well})
    assert well["real"] is None and "Real liquidity not read" in well["reading"]
    assert ">?</text>" in svg
    assert "well-water" not in svg and "well-phantom" not in svg


def test_a_curve_coin_has_no_wall_and_only_a_chain_read_reserve():
    read = liquidity_state(
        coin(venue="bonding_curve", liquidity_usd=971.0, source="Solana (Helius)")
    )
    assert read["lock"] == "curve" and read["real"] == 971.0 and read["quoted"] is None
    quoted_only = liquidity_state(
        coin(venue="bonding_curve", liquidity_usd=971.0, source="GeckoTerminal")
    )
    assert quoted_only["real"] is None


def test_a_liquidity_pull_cracks_the_well():
    pulled = coin(memecoin_assessment={"risk": {"factors": [{"kind": "liquidity_withdrawal"}]}})
    well = liquidity_state(pulled)
    assert well["pulled"] is True and "a liquidity pull is on the record" in well["reading"]
    assert 'class="well-crack"' in well_svg({"band": 1, "liquidity": well})
    assert liquidity_state(coin())["pulled"] is False


def test_the_open_wall_gap_and_risk_notch_draw_only_when_known():
    open_wall = well_svg(
        {
            "band": 2,
            "risk": "unknown",
            "liquidity": liquidity_state(
                coin(liquidity_lock={"left_pct": 40.0, "dex": "Raydium CPMM"})
            ),
        }
    )
    assert '<path class="well-wall" data-lock="open"' in open_wall
    assert "well-risk" not in open_wall
    assert 'data-risk="detected"' in well_svg(
        {"band": 1, "risk": "detected", "liquidity": liquidity_state(coin())}
    )


def test_the_indicator_carries_the_well_and_reads_it_first():
    glyph = memecoin_indicator(coin(attention_score=50, score_components={"market": 50}))
    assert glyph["liquidity"]["lock"] == "sealed"
    assert glyph["description"].startswith(
        "Real liquidity $64.8K; 12.8% of $506.2K fully diluted value"
    )


def test_the_posted_image_draws_the_well_at_one_size():
    low = memecoin_indicator(coin(attention_score=10, score_components={"market": 10}))
    high = memecoin_indicator(coin(attention_score=90, score_components={"market": 90}))
    small, large = glyph_image(low), glyph_image(high)
    assert small.size == large.size  # attention is a gauge, not the size
    centre = small.width // 2
    # 12.8% of fully diluted value is real liquidity: fair, sand water.
    assert small.getpixel((centre, centre))[:3] == (0xE6, 0xBF, 0x6C)
    # The gauge fills to the score: 90 reaches the left side, 10 does not.
    left = (round(centre - 82.5 * 72 / 86), centre)
    assert large.getpixel(left)[:3] != small.getpixel(left)[:3]


def test_the_key_is_drawn_by_the_same_code():
    groups = well_key()
    assert [group["title"] for group in groups] == [
        "Liquidity",
        "Wall",
        "Standards",
        "Attention and trading",
    ]
    assert all(
        svg.startswith('<svg class="well-glyph"')
        for group in groups
        for svg, _, _ in group["items"]
    )


@pytest.mark.parametrize(
    ("real", "depth"),
    [(20_000, "deep"), (15_000, "deep"), (14_999, "fair"), (5_000, "fair"), (4_999, "thin")],
)
def test_depth_is_real_liquidity_against_fully_diluted_value(real, depth):
    well = liquidity_state(coin(real_liquidity_usd=real, fully_diluted_valuation=100_000))
    assert well["depth"] == depth
    assert f'class="well-water" data-depth="{depth}"' in well_svg({"liquidity": well})
    assert liquidity_state(coin(fully_diluted_valuation=None))["depth"] == "unknown"


def test_the_hairline_is_the_last_hours_buyers_against_sellers():
    flow = flow_state({"buyers_h1": 30, "sellers_h1": 10})
    assert (
        flow["share"] == 0.75 and flow["reading"] == "Last hour: 30 buyers, 10 sellers (75% buyers)"
    )
    svg = well_svg({"flow": flow, "liquidity": liquidity_state(coin())})
    assert 'class="well-bull"' in svg and 'class="well-bear"' in svg
    assert flow_state({"buyers_h1": 0, "sellers_h1": 0})["share"] is None
    assert flow_state({})["share"] is None


def test_nine_ticks_carry_the_standards_in_the_rules_order():
    ratification = {
        "standards": [
            {"key": "mint_authority", "label": "Mint", "met": True},
            {"key": "pool", "label": "Pool", "met": False},
            {"key": "holders", "label": "Holders", "met": None},
            {"key": "cash", "label": "Cash", "met": None, "applies": False},
        ]
    }
    standards = standards_state({"ratification": ratification})
    assert [mark["state"] for mark in standards["marks"]] == ["met", "unmet", "unchecked"]
    assert standards["reading"] == "Standards: 1 of 3 met, 1 not checked yet"
    svg = well_svg({"standards": standards, "liquidity": liquidity_state(coin())})
    assert svg.count('class="well-standard"') == 3
    assert standards_state({}) is None


def test_the_gauge_fills_to_the_score_split_by_slice():
    arcs = gauge_arcs(
        {
            "score": 50,
            "slices": [{"key": "market", "share": 0.6}, {"key": "evidence", "share": 0.4}],
        }
    )
    import math

    assert [key for key, _, _ in arcs] == ["market", "evidence"]
    assert arcs[-1][2] - arcs[0][1] == pytest.approx(math.pi)
    assert gauge_arcs({"score": None}) == gauge_arcs({"score": 0}) == []
    # A score with no breakdown still fills, as market.
    assert gauge_arcs({"score": 30, "slices": []})[0][0] == "market"


def test_a_sealed_wall_is_quiet_and_the_indicator_reads_every_mark():
    glyph = memecoin_indicator(
        coin(
            attention_score=50,
            score_components={"market": 50},
            buyers_h1=12,
            sellers_h1=4,
            ratification={"standards": [{"key": "pool", "label": "Pool", "met": True}]},
        )
    )
    assert glyph["flow"]["share"] == 0.75 and glyph["standards"]["met"] == 1
    assert (
        "Last hour: 12 buyers, 4 sellers (75% buyers). Standards: 1 of 1 met."
        in glyph["description"]
    )
    assert 'data-lock="sealed"' in well_svg(glyph)  # drawn, and hidden by the stylesheet
