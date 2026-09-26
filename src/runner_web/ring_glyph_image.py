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
    }


def glyph_outer(indicator: dict[str, Any] | None) -> int:
    """Outer radius in the page's desktop units: low attention draws smaller."""

    return 46 if glyph_state(indicator)["band"] == 1 else 72


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
    size = round(outer if outer is not None else (46 if state["band"] == 1 else 72), 1)
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
