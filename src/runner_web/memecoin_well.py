"""The Well: a memecoin's sigil, drawn around its liquidity.

A memecoin's first question is whether you can get out, so the sigil's centre
answers it. Every circle inside the frame sits on one log scale, $100 at the
centre to $10M at the chamber's widest:

- water: real liquidity, twice the quote side actually in the pool;
- ghost ring: the liquidity the pool quotes; the hatch between it and the
  water is liquidity that is not there;
- chamber: fully diluted value, so a wide empty chamber is a coin valued far
  above what it can be sold into;
- the Ratified line: $10K of real liquidity, the RATi Rules' pool standard;
- wall: the chamber's rim. Sealed when the pool's liquidity tokens are burned
  or locked, open at the bottom by the share still held, dotted when it cannot
  be read, faint when not checked, dashed while the coin is on its curve;
- crack: a liquidity pull on the record.

Attention moves to the frame's thickness and swap balance to its outer
hairline. Radii are computed here once, in units where the frame's outer edge
is 86, so the page (ring-glyph.js), the rows (well_svg) and the images
(ring_glyph_image.py) all scale the same numbers.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from html import escape
from typing import Any

from ratitrust.memecoin import MAX_LP_LEFT_PCT, MIN_LIQUIDITY_USD
from runner_web.attention import finite_number
from runner_web.memecoin_liquidity import CHAIN_SOURCE

FRAME = 86.0
SENTIMENT_R = 93.0
CHAMBER_MIN = 8.0
SCALE_LOW = 100.0
SCALE_DECADES = 5.0
# Frame thickness by attention band (low, medium, high).
BAND_WIDTH = {1: 4.0, 2: 8.0, 3: 14.0}
# Rows and the key draw the Well at about 48px, where a unit is a quarter pixel:
# the same geometry, heavier lines (stroke weights live in stock-indicator.css).
ROW_BAND_WIDTH = {1: 7.0, 2: 11.0, 3: 17.0}
LOCK_STATES = ("sealed", "open", "unread", "unchecked", "curve")
SLICE_COLORS = {
    "market": "var(--indicator-market)",
    "evidence": "var(--indicator-evidence)",
    "social": "var(--indicator-social)",
}


def radius(usd: float | None) -> float:
    """A dollar amount's radius on the shared log scale; nothing reads as zero."""

    if usd is None or usd <= 0:
        return 0.0
    share = (math.log10(usd) - math.log10(SCALE_LOW)) / SCALE_DECADES
    return round(4 + 60 * min(1.0, max(0.0, share)), 3)


def _money(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    if value >= 1e3:
        return f"${value / 1e3:.1f}K"
    return f"${value:,.0f}"


def _pulled(item: Mapping[str, Any]) -> bool:
    assessment = item.get("memecoin_assessment")
    risk = assessment.get("risk") if isinstance(assessment, Mapping) else None
    factors = risk.get("factors") if isinstance(risk, Mapping) else None
    return any(
        isinstance(factor, Mapping) and factor.get("kind") == "liquidity_withdrawal"
        for factor in factors or []
    )


def liquidity_state(item: Mapping[str, Any]) -> dict[str, Any]:
    """The facts the Well draws, with their radii and one plain reading."""

    venue = item.get("venue")
    venue = venue if venue in ("pool", "bonding_curve") else "unknown"
    quoted = finite_number(item.get("liquidity_usd"))
    quoted = quoted if quoted is not None and quoted >= 0 else None
    if venue == "bonding_curve":
        # A curve's own reserve is real SOL only when the chain reader read it.
        real = quoted if item.get("source") == CHAIN_SOURCE else None
        quoted = None
    else:
        real = finite_number(item.get("real_liquidity_usd"))
        real = real if real is not None and real >= 0 else None
    fdv = finite_number(item.get("fully_diluted_valuation"))
    fdv = fdv if fdv is not None and fdv > 0 else None
    lock = item.get("liquidity_lock") if isinstance(item.get("liquidity_lock"), Mapping) else None
    left = finite_number(lock.get("left_pct")) if lock else None
    dex = str(lock.get("dex") or "") if lock else ""
    if venue == "bonding_curve":
        state = "curve"
    elif lock is None:
        state = "unchecked"
    elif left is None:
        state = "unread"
    elif left <= MAX_LP_LEFT_PCT:
        state = "sealed"
    else:
        state = "open"
    pulled = _pulled(item)
    phantom = real is not None and quoted is not None and quoted >= 2 * max(real, 1.0)
    parts = [
        f"Real liquidity {_money(real)}" if real is not None else "Real liquidity not read",
    ]
    if phantom:
        parts.append(f"the pool quotes {_money(quoted)}, {quoted / max(real or 1.0, 1.0):.0f}×")
    if fdv is not None and real is not None:
        parts.append(f"{real / fdv * 100:.1f}% of {_money(fdv)} fully diluted value")
    parts.append(
        {
            "curve": "on its bonding curve, no pool to lock",
            "sealed": f"liquidity tokens burned or locked{f' ({dex})' if dex else ''}",
            "open": f"{left or 0:.0f}% of {dex or 'pool'} liquidity tokens still held",
            "unread": f"{dex or 'this DEX'} liquidity lock not readable",
            "unchecked": "liquidity lock not checked yet",
        }[state]
    )
    if pulled:
        parts.append("a liquidity pull is on the record")
    return {
        "real": real,
        "quoted": quoted,
        "fdv": fdv,
        "venue": venue,
        "lock": state,
        "lock_left_pct": left,
        "lock_dex": dex or None,
        "pulled": pulled,
        "phantom": phantom,
        "line_usd": MIN_LIQUIDITY_USD,
        "radii": {
            "real": radius(real),
            "quoted": radius(quoted),
            "chamber": max(radius(fdv), CHAMBER_MIN),
            "line": radius(MIN_LIQUIDITY_USD),
        },
        "reading": "; ".join(parts) + ".",
    }


def _point(r: float, angle: float) -> str:
    return f"{r * math.cos(angle):.2f} {r * math.sin(angle):.2f}"


def arc(r: float, start: float, end: float) -> str:
    """An SVG arc path, clockwise from `start` to `end` in radians."""

    large = 1 if end - start > math.pi else 0
    return f"M {_point(r, start)} A {r} {r} 0 {large} 1 {_point(r, end)}"


def frame_width(indicator: Mapping[str, Any], widths: Mapping[int, float] = BAND_WIDTH) -> float:
    return widths.get(indicator.get("band"), widths[1])


def well_svg(indicator: Mapping[str, Any], *, size: int = 48) -> str:
    """The Well as inline SVG for rows and keys; the page's map draws it in JS."""

    well = indicator.get("liquidity") or {}
    radii = well.get("radii") or {}
    out = [
        f'<svg class="well-glyph" viewBox="-100 -100 200 200" width="{size}" height="{size}" '
        'aria-hidden="true" focusable="false">'
    ]
    width = frame_width(indicator, ROW_BAND_WIDTH)
    frame = FRAME - width / 2
    out.append(f'<circle class="well-track" r="{frame}" stroke-width="{width}"/>')
    parts = [
        part
        for part in indicator.get("slices") or []
        if part.get("key") in SLICE_COLORS and (part.get("share") or 0) > 0
    ]
    angle = -math.pi / 2
    for part in parts:
        sweep = part["share"] * 2 * math.pi
        color = SLICE_COLORS[part["key"]]
        if sweep >= 2 * math.pi - 1e-6:
            out.append(
                f'<circle class="well-slice" data-key="{part["key"]}" r="{frame}" '
                f'stroke="{color}" stroke-width="{width}"/>'
            )
        else:
            out.append(
                f'<path class="well-slice" data-key="{part["key"]}" '
                f'd="{arc(frame, angle, angle + sweep)}" stroke="{color}" stroke-width="{width}"/>'
            )
        angle += sweep
    mix = indicator.get("sentiment_mix") or {}
    if mix.get("state") == "available":
        bullish = float(mix.get("bullish") or 0)
        start, split = -math.pi / 2, -math.pi / 2 + bullish * 2 * math.pi
        if bullish >= 1:
            out.append(f'<circle class="well-bull" r="{SENTIMENT_R}"/>')
        elif bullish <= 0:
            out.append(f'<circle class="well-bear" r="{SENTIMENT_R}"/>')
        else:
            out.append(f'<path class="well-bull" d="{arc(SENTIMENT_R, start, split)}"/>')
            out.append(
                f'<path class="well-bear" d="{arc(SENTIMENT_R, split, start + 2 * math.pi)}"/>'
            )
    chamber = radii.get("chamber", CHAMBER_MIN)
    water, ghost = radii.get("real", 0), radii.get("quoted", 0)
    out.append(f'<circle class="well-chamber" r="{chamber}"/>')
    if well.get("real") is not None and ghost > water + 1:
        out.append(
            f'<circle class="well-phantom" r="{(ghost + water) / 2:.3f}" '
            f'stroke-width="{ghost - water:.3f}"/>'
        )
    if water > 0:
        out.append(f'<circle class="well-water" r="{water}"/>')
    if ghost and abs(ghost - water) > 1:
        inside = " well-ghost--inside" if ghost < water else ""
        out.append(f'<circle class="well-ghost{inside}" r="{ghost}"/>')
    # At row size the Ratified line is a target: drawn only while the water is below it.
    line = radii.get("line", radius(MIN_LIQUIDITY_USD))
    if water < line:
        out.append(f'<circle class="well-line" r="{line}"/>')
    if well.get("real") is None:
        out.append(
            '<text class="well-unknown" y="2" text-anchor="middle" '
            'dominant-baseline="central">?</text>'
        )
    state = well.get("lock", "unchecked")
    if state == "open":
        gap = min(100.0, max(5.0, well.get("lock_left_pct") or 0)) / 100 * 2 * math.pi
        bottom = math.pi / 2
        if gap < 2 * math.pi - 0.01:
            out.append(
                f'<path class="well-wall" data-lock="open" '
                f'd="{arc(chamber, bottom + gap / 2, bottom - gap / 2 + 2 * math.pi)}"/>'
            )
    else:
        out.append(f'<circle class="well-wall" data-lock="{escape(state)}" r="{chamber}"/>')
    if well.get("pulled"):
        h, bottom = 0.2, math.pi / 2
        out.append(
            f'<path class="well-cut" d="M 0 0 L {_point(chamber + 4, bottom - h)} '
            f'A {chamber + 4} {chamber + 4} 0 0 1 {_point(chamber + 4, bottom + h)} Z"/>'
        )
        zig = [
            (0, 4),
            (5, chamber * 0.35),
            (-4, chamber * 0.6),
            (3, chamber * 0.85),
            (0, chamber + 4),
        ]
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zig)
        out.append(f'<polyline class="well-crack" points="{points}"/>')
    risk = indicator.get("risk")
    # A risk notch at twelve o'clock, the ring's shapes: detected is a diamond,
    # 1+ significant a circle. Unknown risk draws nothing.
    # Twice the map's size, to read at 48px.
    if risk == "detected":
        out.append(
            '<path class="well-risk" data-risk="detected" '
            'd="M 0 -106 L 13 -92 L 0 -78 L -13 -92 Z"/>'
        )
    elif risk == "significant":
        out.append('<circle class="well-risk" data-risk="significant" cy="-92" r="12"/>')
    out.append("</svg>")
    return "".join(out)


def _example(
    band: int = 1,
    slices: tuple = (("market", 1.0),),
    risk: str = "none",
    bullish: float | None = None,
    **facts: Any,
) -> str:
    row = {
        "venue": "pool",
        "real_liquidity_usd": 30000,
        "liquidity_usd": 31000,
        "fully_diluted_valuation": 120000,
        "liquidity_lock": {"left_pct": 0.0, "dex": ""},
    }
    row.update(facts)
    indicator = {
        "band": band,
        "slices": [{"key": key, "share": share} for key, share in slices],
        "risk": risk,
        "sentiment_mix": {"state": "available", "bullish": bullish} if bullish is not None else {},
        "liquidity": liquidity_state(row),
    }
    return well_svg(indicator, size=44)


def well_key() -> list[dict[str, Any]]:
    """The indicator key's memecoin groups, drawn by the same code as the rows."""

    return [
        {
            "title": "Liquidity",
            "note": "One log scale from the centre out: $100 to $10M. Bigger means more dollars.",
            "items": [
                (
                    _example(),
                    "Deep, sealed",
                    "Water past the $10K Ratified line; liquidity tokens burned or locked",
                ),
                (
                    _example(
                        real_liquidity_usd=90,
                        liquidity_usd=2200,
                        fully_diluted_valuation=2100,
                        liquidity_lock=None,
                    ),
                    "Phantom pool",
                    "Hatched: liquidity the pool quotes but does not hold",
                ),
                (
                    _example(
                        real_liquidity_usd=20000,
                        liquidity_usd=21000,
                        fully_diluted_valuation=4_000_000,
                    ),
                    "Thin for its value",
                    "Wide empty chamber: fully diluted value far above real liquidity",
                ),
                (_example(real_liquidity_usd=None), "Not read", "Real liquidity unknown"),
            ],
        },
        {
            "title": "Wall",
            "note": "The chamber's rim: can the liquidity be pulled?",
            "items": [
                (
                    _example(liquidity_lock={"left_pct": 40.0, "dex": ""}),
                    "Open",
                    "The gap is the share of liquidity tokens still held",
                ),
                (
                    _example(liquidity_lock={"left_pct": None, "dex": None}),
                    "Not readable",
                    "Concentrated pools and lock programs",
                ),
                (_example(liquidity_lock=None), "Not checked yet", "Faint rim"),
                (
                    _example(
                        venue="bonding_curve",
                        liquidity_usd=900,
                        fully_diluted_valuation=5800,
                        source=CHAIN_SOURCE,
                    ),
                    "Bonding curve",
                    "No pool yet: dashed chamber",
                ),
                (
                    _example(
                        memecoin_assessment={
                            "risk": {"factors": [{"kind": "liquidity_withdrawal"}]}
                        }
                    ),
                    "Liquidity pull",
                    "A crack when a withdrawal is on the record",
                ),
            ],
        },
        {
            "title": "Frame",
            "note": (
                "Thickness is attention; color is where it came from. "
                "Sampled swap balance runs on the outer hairline."
            ),
            "items": [
                (_example(band=1), "Under 40", "Attention points"),
                (_example(band=2), "40–69.99", "Attention points"),
                (
                    _example(band=3, slices=(("market", 0.7), ("evidence", 0.3))),
                    "70+",
                    "Blue market, purple chain evidence, cyan external social",
                ),
                (_example(bullish=0.75), "▲75% / ▼25%", "Net buying green, net selling dashed red"),
                (
                    _example(risk="detected"),
                    "Risk factors detected",
                    "Diamond at the top · saved checks",
                ),
                (
                    _example(risk="significant"),
                    "1+ significant factors",
                    "Circle at the top · saved checks",
                ),
            ],
        },
    ]
