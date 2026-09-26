"""Render the saved keyframes using the same easing as the details page."""

from __future__ import annotations

import math
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from runner_web.memecoin_chain_parser import short_address
from runner_web.memecoin_replay import POLICY, TIMING, blend, verify_replay
from runner_web.ring_glyph_image import glyph_outer, glyph_state, paste_glyph

SIZE = (800, 600)
CENTER = (400, 300)
# Rings from the outside in: (x radius, y radius, how many nodes fit). The
# inner ring clears the largest attention glyph at the centre.
RINGS = ((350, 205, 24), (262, 158, 16), (178, 114, 8))
# The memecoin page's purple theme (web/static/market-screen.css).
THEME = {
    "bg": "#0d0b11",
    "line": "#2b2535",
    "text": "#f0f4f1",
    "muted": "#96a49b",
    "accent": "#c9aaf7",
    "mark": "#35294d",
    "node_line": "#6a5690",
    "edge": "#4b3d66",
    "up": "#a5e5b9",
    "down": "#ef99a4",
}


def _color(value: str, opacity: float) -> tuple:
    """Blend a theme colour into the background, for nodes fading in or out."""
    value, bg = value.lstrip("#"), THEME["bg"].lstrip("#")
    return tuple(
        round(
            int(bg[i * 2 : i * 2 + 2], 16)
            + (int(value[i * 2 : i * 2 + 2], 16) - int(bg[i * 2 : i * 2 + 2], 16)) * opacity
        )
        for i in range(3)
    )


def _tones(edges: list[dict]) -> dict[str, str]:
    """Colour a node like the stock map: green if it only bought, red if it only sold."""
    roles: dict[str, set[str]] = {}
    for edge in edges:
        for key in (edge["source"], edge["target"]):
            roles.setdefault(key, set()).add(edge["role"])
    return {
        key: "up" if found == {"bought"} else "down" if found == {"sold"} else "neutral"
        for key, found in roles.items()
    }


def ring_positions(ids: list[str]) -> dict[str, tuple[float, float]]:
    """Place nodes on rings around the launch, like the stock map.

    Saved payloads keep their own coordinates because verify_replay checks
    them, so the GIF lays nodes out again at render time. The outer ring fills
    first; each ring spreads its nodes evenly, starting at the top.
    """
    positions, start = {}, 0
    for rx, ry, capacity in RINGS:
        ring = ids[start : start + capacity]
        start += capacity
        for index, key in enumerate(ring):
            angle = -math.pi / 2 + index * 2 * math.pi / len(ring)
            positions[key] = (
                round(CENTER[0] + rx * math.cos(angle), 3),
                round(CENTER[1] + ry * math.sin(angle), 3),
            )
    return positions


def ring_frames(frames: list[dict]) -> list[dict]:
    """Move every node in the saved keyframes onto the ring layout."""
    ids = [n["id"] for n in frames[-1]["nodes"] if n["id"] != "launch"]
    positions = {"launch": CENTER, **ring_positions(ids)}
    return [
        {
            **frame,
            "nodes": [
                {**node, "x": positions[node["id"]][0], "y": positions[node["id"]][1]}
                if node["id"] in positions
                else node
                for node in frame["nodes"]
            ],
        }
        for frame in frames
    ]


def draw_frame(
    payload: dict, frame: dict, progress: float, indicator: dict | None = None
) -> Image.Image:
    image = Image.new("RGB", SIZE, THEME["bg"])
    draw = ImageDraw.Draw(image)
    title = ImageFont.load_default(size=15)
    font = ImageFont.load_default(size=13)
    small = ImageFont.load_default(size=11)
    address = payload["token_address"]
    draw.text((22, 16), "RATi · MEMECOIN", fill=THEME["accent"], font=font)
    draw.text((22, 36), short_address(address), fill=THEME["text"], font=title)
    draw.text((22, 58), address, fill=THEME["muted"], font=small)
    draw.text((778, 18), frame["label"], fill=THEME["text"], font=small, anchor="rt")
    score = glyph_state(indicator)["score"]
    draw.text(
        (778, 38),
        f"Attention {score:.0f} pts" if score is not None else "Attention unavailable",
        fill=THEME["muted"],
        font=small,
        anchor="rt",
    )
    nodes = {n["id"]: n for n in frame["nodes"]}
    tones = _tones(frame["edges"])
    for edge in frame["edges"]:
        a, b = nodes[edge["source"]], nodes[edge["target"]]
        opacity = min(a.get("opacity", 1), b.get("opacity", 1))
        tone = "up" if edge["role"] == "bought" else "down" if edge["role"] == "sold" else "edge"
        draw.line((a["x"], a["y"], b["x"], b["y"]), fill=_color(THEME[tone], opacity), width=2)
    launch = nodes.get("launch")
    for node in frame["nodes"]:
        x, y, radius = node["x"], node["y"], node["r"]
        opacity = node.get("opacity", 1)
        if node["id"] == "launch" or radius < 1 or opacity <= 0:
            continue
        tone = tones.get(node["id"], "neutral")
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=_color(THEME["mark"], opacity),
            outline=_color(THEME[tone] if tone != "neutral" else THEME["node_line"], opacity),
            width=2,
        )
        if radius > 10:
            draw.text(
                (x, y),
                node["address"][:4],
                fill=_color(THEME["text"], opacity),
                font=small,
                anchor="mm",
            )
    if launch and launch["r"] >= 1:
        # The token is the page's attention glyph, grown in with the launch node.
        outer = glyph_outer(indicator) * min(1.0, launch["r"] / 48)
        paste_glyph(
            image,
            (launch["x"], launch["y"]),
            indicator,
            outer=outer,
            background=THEME["bg"],
            line=THEME["line"],
        )
    draw.line((22, 560, 778, 560), fill=THEME["line"], width=3)
    draw.line((22, 560, 22 + progress * 756, 560), fill=THEME["accent"], width=3)
    draw.text(
        (22, 574), "Saved chain evidence · runners.rati.chat", fill=THEME["muted"], font=small
    )
    draw.text(
        (778, 574), "Replay " + payload["id"][:16], fill=THEME["muted"], font=small, anchor="rt"
    )
    return image


def render_gif(payload: dict, indicator: dict | None = None) -> bytes:
    if not verify_replay(payload):
        raise ValueError("Replay quality checks failed")
    frames, durations = [], []

    def append(frame: dict, duration: int, progress: float) -> None:
        # Palette frames keep peak memory bounded even for twelve keyframes.
        image = draw_frame(payload, frame, progress, indicator)
        frames.append(image.quantize(colors=64, method=Image.Quantize.FASTOCTREE))
        durations.append(duration)
        image.close()

    keys = ring_frames(payload["frames"])
    append(keys[0], TIMING["origin_hold"], 0)
    for index, target in enumerate(keys[1:], 1):
        for step in range(1, 17):
            progress = step / 16
            append(
                blend(keys[index - 1], target, progress),
                50,
                (index - 1 + progress) / max(1, len(keys) - 1),
            )
        append(
            target,
            TIMING["final_hold"] if index == len(keys) - 1 else TIMING["hold"],
            index / max(1, len(keys) - 1),
        )
    for step in range(1, 19):
        append(blend(keys[-1], keys[0], step / 18, reset=True), 50, 1 - step / 18)
    try:
        with BytesIO() as output:
            frames[0].save(
                output,
                format="GIF",
                save_all=True,
                append_images=frames[1:],
                duration=durations,
                loop=0,
                disposal=2,
                optimize=False,
            )
            result = output.getvalue()
        if len(result) > POLICY["max_gif_bytes"]:
            raise ValueError("GIF size limit")
        return result
    finally:
        for frame in frames:
            frame.close()
