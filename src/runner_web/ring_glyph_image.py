"""Draw the attention ring glyph into images (Telegram GIFs and share cards).

This mirrors web/static/ring-glyph.js so a posted image reads like the page:
the band sets the size (70+ points is a solid pie), slices keep the page's
colors and patterns, the outer border splits bullish green from striped red,
and the centre marker shows risk (orange circle, red diamond, or "?").
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageFont

SLICE_COLORS = {
    "market": "#418cf4",
    "evidence": "#b58af4",
    "social": "#36d5e6",
    "cluster": "#d9be77",
}
GREEN = "#a5e5b9"
RED = "#ef99a4"
MUTED = "#96a49b"
ORANGE = "#ffad70"
SUPERSAMPLE = 4


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def glyph_state(indicator: dict[str, Any] | None) -> dict[str, Any]:
    """Read an indicator the same way ring-glyph.js does, defaulting to unknown."""

    value = indicator if isinstance(indicator, dict) else {}
    band = value.get("band") if value.get("band") in (1, 2, 3) else 1
    mix = value.get("mix_state") if value.get("mix_state") in ("available", "zero") else "unknown"
    risk = value.get("risk")
    risk = risk if risk in ("none", "detected", "significant") else "unknown"
    parts = []
    for part in value.get("slices") or []:
        if isinstance(part, dict) and part.get("key") in SLICE_COLORS:
            amount = _finite(part.get("value"))
            if amount and amount > 0:
                parts.append((part["key"], amount))
    total = sum(amount for _, amount in parts)
    if mix != "available" or total <= 0:
        parts = []
    sentiment = value.get("sentiment_mix") if isinstance(value.get("sentiment_mix"), dict) else {}
    bullish = _finite(sentiment.get("bullish"))
    bearish = _finite(sentiment.get("bearish"))
    split = (
        bullish
        if sentiment.get("state") == "available"
        and bullish is not None
        and bearish is not None
        and 0 <= bullish <= 1
        and abs(bullish + bearish - 1) < 1e-6
        else None
    )
    return {
        "band": band,
        "mix": mix,
        "risk": risk,
        "slices": [(key, amount / total) for key, amount in parts],
        "bullish": split,
        "score": _finite(value.get("score")),
        "well": _well_state(value),
    }


def _well_state(indicator: dict[str, Any]) -> tuple | None:
    """The Well's drawn facts as a hashable tuple; None for a stock's ring."""

    liquidity = indicator.get("liquidity")
    if not isinstance(liquidity, dict):
        return None
    flow = indicator.get("flow") if isinstance(indicator.get("flow"), dict) else {}
    standards = indicator.get("standards") if isinstance(indicator.get("standards"), dict) else {}
    marks = tuple(
        mark.get("state") if mark.get("state") in ("met", "unmet") else "unchecked"
        for mark in standards.get("marks") or []
        if isinstance(mark, dict)
    )
    depth = liquidity.get("depth")
    radii = liquidity.get("radii") if isinstance(liquidity.get("radii"), dict) else {}
    lock = liquidity.get("lock")
    return (
        ("real", _finite(radii.get("real")) or 0.0),
        ("quoted", _finite(radii.get("quoted")) or 0.0),
        ("chamber", _finite(radii.get("chamber")) or 8.0),
        ("line", _finite(radii.get("line")) or 0.0),
        ("known", liquidity.get("real") is not None),
        (
            "lock",
            lock if lock in ("sealed", "open", "unread", "unchecked", "curve") else "unchecked",
        ),
        ("left", _finite(liquidity.get("lock_left_pct")) or 0.0),
        ("pulled", bool(liquidity.get("pulled"))),
        ("depth", depth if depth in WELL_DEPTH else "unknown"),
        ("flow", _finite(flow.get("share"))),
        ("standards", marks),
    )


def glyph_outer(indicator: dict[str, Any] | None) -> int:
    """Outer radius in the page's desktop units: low attention draws smaller.

    The Well keeps one size; its frame's thickness carries attention instead.
    """

    state = glyph_state(indicator)
    return 72 if state["well"] or state["band"] > 1 else 46


def _hex(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha)


def _dashed_circle(draw, cx, cy, radius, width, color, dash, gap) -> None:
    circumference = 2 * math.pi * radius
    steps = max(1, int(circumference // (dash + gap)))
    sweep = 360 / steps
    on = sweep * dash / (dash + gap)
    box = (
        cx - radius - width / 2,
        cy - radius - width / 2,
        cx + radius + width / 2,
        cy + radius + width / 2,
    )
    for index in range(steps):
        start = -90 + index * sweep
        draw.arc(box, start, start + on, fill=color, width=round(width))


def _pattern(size: int, key: str, ink: tuple, scale: float) -> Image.Image:
    """Tile the page's slice patterns (evidence stripes, social dots, cluster grid)."""

    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    step = 7 * scale
    if key == "evidence":
        # 45-degree stripes, like the SVG pattern rotated by 45.
        offset = -size
        while offset < size:
            draw.line((offset, size, offset + size, 0), fill=ink, width=max(1, round(2.2 * scale)))
            offset += step * math.sqrt(2)
    elif key == "social":
        y = step / 2
        while y < size:
            x = step / 2
            while x < size:
                r = 1.4 * scale
                draw.ellipse((x - r, y - r, x + r, y + r), fill=ink)
                x += step
            y += step
    elif key == "cluster":
        grid = 8 * scale
        position = 0.0
        while position < size:
            draw.line((position, 0, position, size), fill=ink, width=max(1, round(1.6 * scale)))
            draw.line((0, position, size, position), fill=ink, width=max(1, round(1.6 * scale)))
            position += grid
    return layer


def _render(state_key: tuple, outer: float, background: str, line: str) -> Image.Image:
    state = dict(state_key)
    if state.get("well"):
        return _render_well(state, outer, background, line)
    slices = list(state["slices"])
    s = SUPERSAMPLE
    scale = outer / (72 if state["band"] > 1 else 46)
    width = 20 * scale
    radius = outer - width / 2
    margin = 12 * scale
    half = outer + margin
    size = math.ceil(2 * half * s)
    cx = cy = size / 2
    ink = _hex(background)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def ring_box(r: float, w: float) -> tuple[float, float, float, float]:
        return (
            cx - (r + w / 2) * s,
            cy - (r + w / 2) * s,
            cx + (r + w / 2) * s,
            cy + (r + w / 2) * s,
        )

    # Track: a full band for a saved breakdown; a thin (dashed if unknown) ring otherwise.
    if state["mix"] == "available":
        draw.arc(ring_box(radius, width), 0, 360, fill=_hex(line), width=round(width * s))
    elif state["mix"] == "zero":
        draw.arc(ring_box(radius, 1), 0, 360, fill=_hex(MUTED), width=s)
    else:
        _dashed_circle(draw, cx, cy, radius * s, s, _hex(MUTED), 4 * s, 4 * s)

    solid = state["band"] == 3
    angle = -90.0
    boundaries = []
    for key, share in slices:
        sweep = share * 360
        mask = Image.new("L", (size, size), 0)
        shape = ImageDraw.Draw(mask)
        if solid:
            box = (cx - outer * s, cy - outer * s, cx + outer * s, cy + outer * s)
            if len(slices) == 1:
                shape.ellipse(box, fill=255)
            else:
                shape.pieslice(box, angle, angle + sweep, fill=255)
        else:
            shape.arc(
                ring_box(radius, width), angle, angle + sweep, fill=255, width=round(width * s)
            )
        image.paste(Image.new("RGBA", (size, size), _hex(SLICE_COLORS[key])), (0, 0), mask)
        if key in ("evidence", "social", "cluster"):
            image.paste(
                _pattern(size, key, ink, scale * s),
                (0, 0),
                _mask_and(mask, _pattern(size, key, ink, scale * s)),
            )
        if len(slices) > 1:
            boundaries.append(angle)
        angle += sweep
    inner = 0 if solid else radius - width / 2
    for start in boundaries:
        radians = math.radians(start)
        draw.line(
            (
                cx + inner * s * math.cos(radians),
                cy + inner * s * math.sin(radians),
                cx + outer * s * math.cos(radians),
                cy + outer * s * math.sin(radians),
            ),
            fill=ink,
            width=round(2 * s),
        )

    # Sentiment border: green bullish share from the top, then striped red.
    border = outer + 6 * scale
    if state["bullish"] is not None:
        bullish = state["bullish"] * 360
        box = ring_box(border, 4 * scale)
        stroke = max(1, round(4 * scale * s))
        if bullish > 0:
            draw.arc(box, -90, -90 + bullish, fill=_hex(GREEN), width=stroke)
        if bullish < 360:
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).arc(box, -90 + bullish, 270, fill=255, width=stroke)
            image.paste(Image.new("RGBA", (size, size), _hex(RED)), (0, 0), mask)
            stripes = _pattern(size, "evidence", ink, scale * s)
            image.paste(stripes, (0, 0), _mask_and(mask, stripes))
    else:
        _dashed_circle(
            draw, cx, cy, border * s, 3 * scale * s, _hex(MUTED), 7 * scale * s, 6 * scale * s
        )

    if not solid or not slices:
        hole = radius - width / 2 - 3 * scale
        draw.ellipse((cx - hole * s, cy - hole * s, cx + hole * s, cy + hole * s), fill=ink)

    if state["risk"] != "none":
        marker = 9 * scale * s
        edge = max(1, round(2 * scale * s))
        if state["risk"] == "detected":
            point = marker + 2 * scale * s
            draw.polygon(
                ((cx, cy - point), (cx + point, cy), (cx, cy + point), (cx - point, cy)),
                fill=_hex(RED),
                outline=ink,
                width=edge,
            )
        elif state["risk"] == "significant":
            draw.ellipse(
                (cx - marker, cy - marker, cx + marker, cy + marker),
                fill=_hex(ORANGE),
                outline=ink,
                width=edge,
            )
        else:
            draw.ellipse(
                (cx - marker, cy - marker, cx + marker, cy + marker),
                fill=ink,
                outline=_hex(MUTED),
                width=max(1, s),
            )
            font = ImageFont.load_default(size=max(8, round(12 * scale * s)))
            draw.text((cx, cy), "?", fill=_hex(MUTED), font=font, anchor="mm")
    final = max(1, round(2 * half))
    return image.resize((final, final), Image.Resampling.LANCZOS)


WATER = "#b9c4be"
WALL = "#e4ece7"
# Real liquidity against fully diluted value (memecoin_well.DEPTHS).
WELL_DEPTH = {"deep": "#4fcaa6", "fair": "#e6bf6c", "thin": "#ef8778"}
WELL_GAUGE = 9.0
STANDARD_TONES = {"met": WALL, "unmet": RED}


def _mix(a: str, b: str, amount: float) -> tuple[int, int, int, int]:
    x, y = _hex(a), _hex(b)
    return tuple(round(x[i] + (y[i] - x[i]) * amount) for i in range(3)) + (255,)  # type: ignore[return-value]


def _dashed_arc(draw, box, start: float, end: float, width: int, color, dash: float, gap: float):
    """Dashes along an arc, in degrees clockwise from three o'clock."""

    angle = start
    while angle < end:
        draw.arc(box, angle, min(end, angle + dash), fill=color, width=width)
        angle += dash + gap


def _render_well(state: dict, outer: float, background: str, line: str) -> Image.Image:
    """The Well, mirroring memecoin_well.well_svg: frame edge 86 units, all radii from it."""

    well = dict(state["well"])
    s = SUPERSAMPLE
    unit = outer / 86 * s
    half = 104 * outer / 86
    size = math.ceil(2 * half * s)
    c = size / 2
    ink = _hex(background)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def box(r: float) -> tuple[float, float, float, float]:
        return (c - r * unit, c - r * unit, c + r * unit, c + r * unit)

    def stroke(w: float) -> int:
        return max(1, round(w * unit))

    # Frame: an attention gauge filled clockwise to score/100, split by slice.
    ring = box(86)  # PIL draws an arc's width inward from its box
    draw.arc(ring, 0, 360, fill=_hex(line), width=stroke(WELL_GAUGE))
    sweep_all = min(100.0, max(0.0, state["score"] or 0.0)) * 3.6
    angle = -90.0
    parts = (state["slices"] or [("market", 1.0)]) if sweep_all else []
    for key, share in parts:
        sweep = share * sweep_all
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).arc(ring, angle, angle + sweep, fill=255, width=stroke(WELL_GAUGE))
        image.paste(Image.new("RGBA", (size, size), _hex(SLICE_COLORS[key])), (0, 0), mask)
        if key in ("evidence", "social"):
            pattern = _pattern(size, key, ink, unit / 1.2)
            image.paste(pattern, (0, 0), _mask_and(mask, pattern))
        angle += sweep
    # The last hour's buyers (green) against its sellers (dashed red).
    if well["flow"] is not None:
        hair = box(94.25)
        split = -90 + well["flow"] * 360
        if well["flow"] > 0:
            draw.arc(hair, -90, split, fill=_hex(GREEN), width=stroke(2.5))
        if well["flow"] < 1:
            _dashed_arc(draw, hair, split, 270, stroke(2.5), _hex(RED), 2.2, 1.6)
    # Nine ticks, one per RATi standard.
    marks = well["standards"]
    if marks:
        step = 360 / len(marks)
        gap = min(9.0, step * 0.3)
        for index, mark in enumerate(marks):
            start = -90 + index * step + gap / 2
            tone = (
                _hex(STANDARD_TONES[mark])
                if mark in STANDARD_TONES
                else _mix(background, MUTED, 0.22)
            )
            draw.arc(box(75.5), start, start + step - gap, fill=tone, width=stroke(5))
    chamber, water, ghost = well["chamber"], well["real"], well["quoted"]
    tint = WELL_DEPTH.get(well["depth"])
    draw.ellipse(box(chamber), fill=_mix(background, tint or WALL, 0.32 if tint else 0.1))
    if well["known"] and ghost > water + 1:
        mask = Image.new("L", (size, size), 0)
        shape = ImageDraw.Draw(mask)
        shape.ellipse(box(ghost), fill=255)
        shape.ellipse(box(water), fill=0)
        hatch = _pattern(size, "evidence", _mix(background, MUTED, 0.6), unit / 1.4)
        image.paste(hatch, (0, 0), _mask_and(mask, hatch))
    if water > 0:
        draw.ellipse(box(water), fill=_hex(tint or WATER))
    if ghost and abs(ghost - water) > 1:
        tone = ink if ghost < water else _hex(WALL)
        _dashed_circle(draw, c, c, ghost * unit, unit, tone, 3 * unit, 2.5 * unit)
    if well["line"]:
        tone = ink if water >= well["line"] else _hex(MUTED)
        _dashed_circle(draw, c, c, well["line"] * unit, max(1, unit * 0.8), tone, unit, 2.4 * unit)
    if not well["known"]:
        font = ImageFont.load_default(size=max(8, round(22 * unit)))
        draw.text((c, c), "?", fill=_hex(MUTED), font=font, anchor="mm")
    lock = well["lock"]
    # A sealed wall draws nothing: it is the usual state. Only an open one is loud.
    if lock == "open":
        gap = min(100.0, max(5.0, well["left"])) * 3.6
        if gap < 359:
            draw.arc(
                box(chamber + 1.6),
                90 + gap / 2,
                450 - gap / 2,
                fill=_hex(ORANGE),
                width=stroke(3.2),
            )
    elif lock == "unread":
        _dashed_arc(draw, box(chamber + 1.1), 0, 360, stroke(2.2), _hex(MUTED), 1.2, 6)
    elif lock == "curve":
        _dashed_arc(draw, box(chamber + 0.75), 0, 360, stroke(1.5), _hex(MUTED), 7, 5)
    else:
        draw.arc(box(chamber + 0.5), 0, 360, fill=_hex(line), width=stroke(1))
    if well["pulled"]:
        r = chamber + 4
        draw.pieslice(box(r), 90 - 11.5, 90 + 11.5, fill=ink)
        zig = [(0, 4), (5, chamber * 0.35), (-4, chamber * 0.6), (3, chamber * 0.85), (0, r)]
        draw.line(
            [(c + x * unit, c + y * unit) for x, y in zig],
            fill=_hex(RED),
            width=stroke(2.2),
            joint="curve",
        )
    # A risk factor notches the top. Unknown risk draws nothing: the centre's
    # "?" is kept for unknown liquidity.
    if state["risk"] == "detected":
        notch = [(0, -101), (7, -93), (0, -85), (-7, -93)]
        points = [(c + x * unit, c + y * unit) for x, y in notch]
        draw.polygon(points, fill=_hex(RED), outline=ink, width=stroke(2))
    elif state["risk"] == "significant":
        draw.ellipse(
            (c - 7 * unit, c - 100 * unit, c + 7 * unit, c - 86 * unit),
            fill=_hex(ORANGE),
            outline=ink,
            width=stroke(2),
        )
    final = max(1, round(2 * half))
    return image.resize((final, final), Image.Resampling.LANCZOS)


def _mask_and(mask: Image.Image, pattern: Image.Image) -> Image.Image:
    """Alpha of the pattern, limited to where the slice mask is drawn."""

    from PIL import ImageChops

    return ImageChops.multiply(mask, pattern.getchannel("A"))


@lru_cache(maxsize=64)
def _cached(state_key: tuple, outer: float, background: str, line: str) -> Image.Image:
    return _render(state_key, outer, background, line)


def glyph_image(
    indicator: dict[str, Any] | None,
    *,
    outer: float | None = None,
    background: str = "#0b100e",
    line: str = "#23302a",
) -> Image.Image:
    """An RGBA glyph centred in its image; outer defaults to the band's size."""

    state = glyph_state(indicator)
    key = tuple(sorted({**state, "slices": tuple(state["slices"])}.items()))
    size = round(outer if outer is not None else glyph_outer(indicator), 1)
    return _cached(key, size, background, line)


def paste_glyph(
    image: Image.Image,
    center: tuple[float, float],
    indicator: dict[str, Any] | None,
    **options: Any,
) -> None:
    glyph = glyph_image(indicator, **options)
    x = round(center[0] - glyph.width / 2)
    y = round(center[1] - glyph.height / 2)
    image.paste(glyph, (x, y), glyph)
