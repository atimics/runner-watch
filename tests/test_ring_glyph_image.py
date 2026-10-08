"""The posted glyph reads like the page's: size, slices, sentiment and risk."""

from io import BytesIO

from PIL import Image

from runner_web.memecoin_replay_gif import THEME, draw_frame, ring_frames
from runner_web.ring_glyph_image import (
    GREEN,
    ORANGE,
    RED,
    SLICE_COLORS,
    glyph_image,
    glyph_state,
)
from tests.test_memecoin_replay import payload


def indicator(band=2, risk="detected", bullish=0.5, **extra):
    return {
        "band": band,
        "mix_state": "available",
        "slices": [{"key": "market", "value": 30}, {"key": "evidence", "value": 10}],
        "sentiment_mix": {"state": "available", "bullish": bullish, "bearish": 1 - bullish},
        "risk": risk,
        "score": 55,
        **extra,
    }


def near(pixel, color, tolerance=40):
    target = tuple(int(color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], target, strict=True))


def test_unknown_values_stay_unknown():
    state = glyph_state({"band": 9, "risk": "odd", "slices": [{"key": "market", "value": 5}]})
    assert state == {
        "band": 1,
        "mix": "unknown",
        "risk": "unknown",
        "slices": [],
        "bullish": None,
        "score": None,
        "well": None,
    }


def test_band_sets_size_and_high_attention_is_a_solid_pie():
    small = glyph_image(indicator(band=1))
    large = glyph_image(indicator(band=3, risk="none"))
    assert small.width < large.width
    middle = large.width // 2
    # Solid band: the market slice fills in toward the centre, with no hole.
    assert near(large.getpixel((middle + 20, middle - 5)), SLICE_COLORS["market"])


def test_risk_marker_and_sentiment_border_match_the_page():
    detected = glyph_image(indicator(risk="detected"))
    centre = detected.width // 2
    assert near(detected.getpixel((centre, centre)), RED)
    significant = glyph_image(indicator(risk="significant"))
    assert near(significant.getpixel((centre, centre)), ORANGE)
    bullish = glyph_image(indicator(bullish=1.0, risk="none"))
    # Top of the outer border is the bullish (green) side.
    top = next(y for y in range(bullish.height) if bullish.getpixel((centre, y))[3] > 200)
    assert near(bullish.getpixel((centre, top + 1)), GREEN)


def test_gif_centres_the_attention_glyph_in_the_memecoin_theme():
    data = payload()
    frame = ring_frames(data["frames"])[-1]
    image = draw_frame(data, frame, 1, indicator(risk="detected"))
    assert near(image.getpixel((5, 5)), THEME["bg"], 4)
    launch = next(node for node in frame["nodes"] if node["id"] == "launch")
    assert near(image.getpixel((round(launch["x"]), round(launch["y"]))), RED)


def test_share_cards_carry_the_glyph():
    from runner_web.share_cards import _memecoin_card_png

    coin = {
        "token_address": "So11111111111111111111111111111111111111112",
        "price": 1,
        "attention_score": 30,
        "score_components": {"market": 30},
    }
    with Image.open(BytesIO(_memecoin_card_png({"coin": coin, "history": []}))) as card:
        rgb = card.convert("RGB")
        # Purple frame, and the market slice on the Well's frame: its outer edge is
        # 0.9 × 72 px, and low attention draws it 4 of 86 units thick.
        assert near(rgb.getpixel((600, 56)), "#c9aaf7")
        assert near(rgb.getpixel((1020 + 63, 406)), SLICE_COLORS["market"])


def test_a_coin_in_several_pools_draws_its_wedges():
    from runner_web.memecoin_well import liquidity_state

    pools = [
        {"dex": "meteora", "quote": "SOL", "real_liquidity_usd": 30_000},
        {"dex": "meteora", "quote": "RATI", "real_liquidity_usd": 30_000},
    ]
    row = {"venue": "pool", "fully_diluted_valuation": 200_000, "pools": pools}
    split = glyph_image({**indicator(risk="none"), "liquidity": liquidity_state(row)})
    whole = glyph_image(
        {
            **indicator(risk="none"),
            "liquidity": liquidity_state({**row, "pools": None, "real_liquidity_usd": 60_000}),
        }
    )
    c = split.width // 2
    right, left = split.getpixel((c + c // 6, c)), split.getpixel((c - c // 6, c))

    # Two even pools: the right half is plain water, the left half shaded.
    assert near(right, "#4fcaa6") and not near(left, "#4fcaa6", tolerance=12)
    # One pool, the same total: plain water on both sides.
    assert near(whole.getpixel((c - c // 6, c)), "#4fcaa6")
