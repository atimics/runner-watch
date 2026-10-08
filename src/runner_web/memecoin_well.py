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
- crack: a liquidity pull on the record;
- depth color: real liquidity against fully diluted value tints the water and
  chamber, teal when 15% or more of the value could be sold into, sand from 5%,
  coral below.

Around it: nine ticks for the RATi standards (bright met, red failed, dim not
checked yet; nine bright is Ratified), the frame as an attention gauge filled
to score/100 in the colors of where attention came from, an outer hairline
splitting the last hour's buyers from its sellers, and a risk notch at the top.
Rows print the attention score under the glyph.

Radii are computed here once, in units where the frame's outer edge is 86, so
the page (ring-glyph.js), the rows (well_svg) and the images
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
STANDARDS_R = 73.0
CHAMBER_MIN = 8.0
CHAMBER_MAX = 64.0
# The frame is an attention gauge: filled clockwise from the top to score/100.
GAUGE_WIDTH = 6.0
# Real liquidity as a share of fully diluted value: how much could be sold into.
# Deep also needs the Ratified pool's $10K: a $900 pool that is most of a
# $3,000 coin is still too small to sell into, so it is fair at best.
DEPTHS = ((0.15, "deep"), (0.05, "fair"), (0.0, "thin"))
SCALE_LOW = 100.0
SCALE_DECADES = 5.0
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
    return round(4 + (CHAMBER_MAX - 4) * min(1.0, max(0.0, share)), 3)


def _money(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    if value >= 1e3:
        return f"${value / 1e3:.1f}K"
    return f"${value:,.0f}"


DEX_NAMES = {
    "pumpswap": "PumpSwap",
    "meteora": "Meteora",
    "raydium": "Raydium",
    "orca": "Orca",
    "others": "Others",
}


def _pools(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The coin's pools as wedges, busiest first; empty unless it trades in two or more."""

    pools = []
    for pool in item.get("pools") or []:
        if not isinstance(pool, Mapping):
            continue
        real = finite_number(pool.get("real_liquidity_usd"))
        if real is None or real <= 0:
            continue
        quoted = finite_number(pool.get("liquidity_usd"))
        dex = str(pool.get("dex") or "")
        quote = str(pool.get("quote") or "")
        label = " ".join(filter(None, (DEX_NAMES.get(dex, dex.title() or "Pool"), quote)))
        pools.append(
            {
                "label": label,
                "real": real,
                "quoted": quoted if quoted is not None and quoted >= 0 else real,
            }
        )
    if len(pools) < 2:
        return []
    total = sum(pool["real"] for pool in pools)
    return [{**pool, "share": pool["real"] / total} for pool in pools]


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
    # Several pools: the water is all of them, one wedge each.
    pools = _pools(item) if venue == "pool" else []
    if pools:
        real = sum(pool["real"] for pool in pools)
        quoted = sum(pool["quoted"] for pool in pools)
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
    if pools:
        split = ", ".join(f"{pool['label']} {pool['share'] * 100:.0f}%" for pool in pools)
        parts[0] += f" across {len(pools)} pools ({split})"
    if phantom:
        parts.append(f"the pool quotes {_money(quoted)}, {quoted / max(real or 1.0, 1.0):.0f}×")
    backing = real / fdv if fdv is not None and real is not None else None
    depth = (
        next(name for floor, name in DEPTHS if backing >= floor)
        if backing is not None
        else "unknown"
    )
    if depth == "deep" and (real or 0) < MIN_LIQUIDITY_USD:
        depth = "fair"
    if backing is not None:
        parts.append(f"{backing * 100:.1f}% of {_money(fdv)} fully diluted value ({depth})")
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
        "backing": backing,
        "depth": depth,
        "line_usd": MIN_LIQUIDITY_USD,
        "pools": [{"label": pool["label"], "share": round(pool["share"], 4)} for pool in pools],
        "radii": {
            "real": radius(real),
            "quoted": radius(quoted),
            "chamber": max(radius(fdv), CHAMBER_MIN),
            "line": radius(MIN_LIQUIDITY_USD),
        },
        "reading": "; ".join(parts) + ".",
    }


def flow_state(item: Mapping[str, Any]) -> dict[str, Any]:
    """The last hour's buyers against its sellers: every coin's quote carries them."""

    buyers = finite_number(item.get("buyers_h1"))
    sellers = finite_number(item.get("sellers_h1"))
    if buyers is None or sellers is None or buyers < 0 or sellers < 0 or buyers + sellers == 0:
        return {
            "buyers": None,
            "sellers": None,
            "share": None,
            "reading": "No trades read in the last hour",
        }
    share = buyers / (buyers + sellers)
    return {
        "buyers": int(buyers),
        "sellers": int(sellers),
        "share": round(share, 4),
        "reading": (
            f"Last hour: {int(buyers):,} buyers, {int(sellers):,} sellers "
            f"({share * 100:.0f}% buyers)"
        ),
    }


def standards_state(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """Each applicable RATi standard as met, unmet or unchecked, in the rules' order."""

    ratification = item.get("ratification")
    if not isinstance(ratification, Mapping):
        return None
    marks = []
    for standard in ratification.get("standards") or []:
        if not isinstance(standard, Mapping) or standard.get("applies") is False:
            continue
        met = standard.get("met")
        marks.append(
            {
                "key": str(standard.get("key") or ""),
                "label": str(standard.get("label") or ""),
                "state": "met" if met is True else "unmet" if met is False else "unchecked",
            }
        )
    if not marks:
        return None
    met = sum(mark["state"] == "met" for mark in marks)
    unchecked = sum(mark["state"] == "unchecked" for mark in marks)
    reading = f"Standards: {met} of {len(marks)} met"
    if unchecked:
        reading += f", {unchecked} not checked yet"
    return {"marks": marks, "met": met, "total": len(marks), "reading": reading}


def _point(r: float, angle: float) -> str:
    return f"{r * math.cos(angle):.2f} {r * math.sin(angle):.2f}"


def arc(r: float, start: float, end: float) -> str:
    """An SVG arc path, clockwise from `start` to `end` in radians."""

    large = 1 if end - start > math.pi else 0
    return f"M {_point(r, start)} A {r} {r} 0 {large} 1 {_point(r, end)}"


def gauge_arcs(indicator: Mapping[str, Any]) -> list[tuple[str, float, float]]:
    """(slice key, start, end) for the attention gauge: score/100 of the circle,
    split by each slice's share."""

    score = finite_number(indicator.get("score"))
    if score is None or score <= 0:
        return []
    sweep = min(100.0, score) / 100 * 2 * math.pi
    parts = [
        (part["key"], float(part.get("share") or 0))
        for part in indicator.get("slices") or []
        if part.get("key") in SLICE_COLORS and (part.get("share") or 0) > 0
    ] or [("market", 1.0)]
    arcs, angle = [], -math.pi / 2
    for key, share in parts:
        arcs.append((key, angle, angle + share * sweep))
        angle += share * sweep
    return arcs


def well_svg(indicator: Mapping[str, Any], *, size: int = 56) -> str:
    """The Well as inline SVG for rows and keys; the page's map draws it in JS."""

    well = indicator.get("liquidity") or {}
    radii = well.get("radii") or {}
    out = [
        f'<svg class="well-glyph" viewBox="-100 -100 200 200" width="{size}" height="{size}" '
        'aria-hidden="true" focusable="false">'
    ]
    gauge = FRAME - GAUGE_WIDTH / 2
    out.append(f'<circle class="well-track" r="{gauge}" stroke-width="{GAUGE_WIDTH}"/>')
    for key, start, end in gauge_arcs(indicator):
        shape = (
            f'<circle class="well-slice" data-key="{key}" r="{gauge}"'
            if end - start >= 2 * math.pi - 1e-6
            else f'<path class="well-slice" data-key="{key}" d="{arc(gauge, start, end)}"'
        )
        out.append(f'{shape} stroke="{SLICE_COLORS[key]}" stroke-width="{GAUGE_WIDTH}"/>')
    # The last hour's buyers (green, from the top) against its sellers (dashed red).
    share = (indicator.get("flow") or {}).get("share")
    if share is not None:
        start, split = -math.pi / 2, -math.pi / 2 + share * 2 * math.pi
        if share >= 1:
            out.append(f'<circle class="well-bull" r="{SENTIMENT_R}"/>')
        elif share <= 0:
            out.append(f'<circle class="well-bear" r="{SENTIMENT_R}"/>')
        else:
            out.append(f'<path class="well-bull" d="{arc(SENTIMENT_R, start, split)}"/>')
            out.append(
                f'<path class="well-bear" d="{arc(SENTIMENT_R, split, start + 2 * math.pi)}"/>'
            )
    out.extend(_standards_ticks(indicator.get("standards")))
    chamber = radii.get("chamber", CHAMBER_MIN)
    water, ghost = radii.get("real", 0), radii.get("quoted", 0)
    depth = escape(str(well.get("depth") or "unknown"))
    out.append(f'<circle class="well-chamber" data-depth="{depth}" r="{chamber}"/>')
    if well.get("real") is not None and ghost > water + 1:
        out.append(
            f'<circle class="well-phantom" r="{(ghost + water) / 2:.3f}" '
            f'stroke-width="{ghost - water:.3f}"/>'
        )
    if water > 0:
        out.append(f'<circle class="well-water" data-depth="{depth}" r="{water}"/>')
        out.extend(_wedges(well.get("pools"), water))
    if ghost and abs(ghost - water) > 1:
        inside = " well-ghost--inside" if ghost < water else ""
        out.append(f'<circle class="well-ghost{inside}" r="{ghost}"/>')
    # At row size the Ratified line is a target: drawn only while the water is below it.
    line = radii.get("line", radius(MIN_LIQUIDITY_USD))
    if water < line:
        out.append(f'<circle class="well-line" r="{line}"/>')
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
    if well.get("real") is None:
        out.append(
            '<text class="well-unknown" y="2" text-anchor="middle" '
            'dominant-baseline="central">?</text>'
        )
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


def pool_wedges(pools: Any) -> list[tuple[float, float]]:
    """Each pool's (start, end) angle, clockwise from the top, busiest first."""

    shares = [
        share
        for pool in pools or []
        if isinstance(pool, Mapping)
        and (share := finite_number(pool.get("share"))) is not None
        and share > 0
    ]
    if len(shares) < 2:
        return []
    total, angle, spans = sum(shares), -math.pi / 2, []
    for share in shares:
        end = angle + share / total * 2 * math.pi
        spans.append((angle, end))
        angle = end
    return spans


def _wedges(pools: Any, water: float) -> list[str]:
    """Every other pool's wedge shaded, and a cut between neighbours."""

    out = []
    spans = pool_wedges(pools)
    for index, (start, end) in enumerate(spans):
        if index % 2:
            large = 1 if end - start > math.pi else 0
            out.append(
                f'<path class="well-wedge" d="M 0 0 L {_point(water, start)} '
                f'A {water} {water} 0 {large} 1 {_point(water, end)} Z"/>'
            )
    for start, _ in spans:
        x, y = water * math.cos(start), water * math.sin(start)
        out.append(f'<line class="well-split" x1="0" y1="0" x2="{x:.2f}" y2="{y:.2f}"/>')
    return out


def standards_arcs(count: int) -> list[tuple[float, float]]:
    """Start and end angles of each standard's tick, clockwise from the top."""

    if count <= 0:
        return []
    step = 2 * math.pi / count
    gap = min(0.16, step * 0.3)
    return [
        (-math.pi / 2 + index * step + gap / 2, -math.pi / 2 + (index + 1) * step - gap / 2)
        for index in range(count)
    ]


def _standards_ticks(standards: Mapping[str, Any] | None) -> list[str]:
    marks = (standards or {}).get("marks") or []
    return [
        f'<path class="well-standard" data-state="{escape(mark["state"])}" '
        f'd="{arc(STANDARDS_R, start, end)}"/>'
        for mark, (start, end) in zip(marks, standards_arcs(len(marks)), strict=True)
    ]


def _example(
    band: int = 1,
    slices: tuple = (("market", 1.0),),
    risk: str = "none",
    buyers: tuple[int, int] | None = None,
    score: float | None = None,
    standards: str = "",
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
    states = {"m": True, "u": False, "c": None}
    item = {
        "buyers_h1": buyers[0] if buyers else None,
        "sellers_h1": buyers[1] if buyers else None,
        "ratification": {
            "standards": [{"key": f"s{i}", "met": states[c]} for i, c in enumerate(standards)]
        },
    }
    indicator = {
        "band": band,
        "score": score,
        "slices": [{"key": key, "share": share} for key, share in slices],
        "risk": risk,
        "flow": flow_state(item),
        "standards": standards_state(item),
        "liquidity": liquidity_state(row),
    }
    return well_svg(indicator, size=48)


def well_key() -> list[dict[str, Any]]:
    """The indicator key's memecoin groups, drawn by the same code as the rows."""

    return [
        {
            "title": "Liquidity",
            "note": (
                "One log scale from the centre out: $100 to $10M. Color is real liquidity "
                "against fully diluted value: how much of the price could be sold into."
            ),
            "items": [
                (
                    _example(),
                    "Deep",
                    "Teal: 15%+ of fully diluted value is real liquidity, and $10K or more",
                ),
                (
                    _example(fully_diluted_valuation=300000),
                    "Fair",
                    "Sand: 5–15%, or a deep pool under $10K",
                ),
                (
                    _example(real_liquidity_usd=20000, fully_diluted_valuation=4_000_000),
                    "Thin",
                    "Coral: under 5%; a wide, empty chamber",
                ),
                (
                    _example(
                        pools=[
                            {"dex": "meteora", "quote": "SOL", "real_liquidity_usd": 18000},
                            {"dex": "meteora", "quote": "RATI", "real_liquidity_usd": 8000},
                            {"dex": "meteora", "quote": "USDC", "real_liquidity_usd": 4000},
                        ]
                    ),
                    "Several pools",
                    "One wedge per pool, sized by its share of the real liquidity",
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
                (_example(real_liquidity_usd=None), "Not read", "Real liquidity unknown"),
            ],
        },
        {
            "title": "Wall",
            "note": (
                "The chamber's rim: can the liquidity be pulled? "
                "No rim means sealed: burned or locked."
            ),
            "items": [
                (
                    _example(liquidity_lock={"left_pct": 40.0, "dex": ""}),
                    "Open",
                    "Orange; the gap is the share of liquidity tokens still held",
                ),
                (
                    _example(liquidity_lock={"left_pct": None, "dex": None}),
                    "Not readable",
                    "Concentrated pools and lock programs",
                ),
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
            "title": "Standards",
            "note": "Nine ticks, one per RATi standard, clockwise from the top.",
            "items": [
                (_example(standards="mmmmmmmmm"), "Ratified", "All nine met"),
                (_example(standards="mmmmucmmc"), "Partly met", "Bright met, red failed"),
                (_example(standards="mmmcccccc"), "Not checked yet", "Dim ticks"),
            ],
        },
        {
            "title": "Attention and trading",
            "note": (
                "The frame is an attention gauge, filled clockwise from the top to the "
                "score out of 100; its colors show where attention came from."
            ),
            "items": [
                (_example(score=24), "24 of 100", "Attention points"),
                (
                    _example(score=82, slices=(("market", 0.7), ("evidence", 0.3))),
                    "82 of 100",
                    "Blue market, purple chain evidence, cyan external social",
                ),
                (
                    _example(buyers=(30, 10)),
                    "75% buyers",
                    "Outer hairline: last hour's buyers green, sellers dashed red",
                ),
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
