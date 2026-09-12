"""Render the saved keyframes using the same easing as the details page."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from runner_web.memecoin_replay import COLORS, POLICY, TIMING, blend, verify_replay

SIZE = (800, 600)
BACKGROUND = "#131c22"


def _color(value: str, opacity: float) -> tuple:
    value = value.lstrip("#")
    bg = (19, 28, 34)
    return tuple(
        round(bg[i] + (int(value[i * 2 : i * 2 + 2], 16) - bg[i]) * opacity) for i in range(3)
    )


def draw_frame(payload: dict, frame: dict, progress: float) -> Image.Image:
    image = Image.new("RGB", SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=13)
    small = ImageFont.load_default(size=11)
    draw.text((22, 18), "RATi RUNNERS / TOKEN EVENT NETWORK", fill=COLORS["launch"], font=font)
    draw.text((22, 41), payload["token_address"], fill="#afbec4", font=small)
    draw.text((778, 20), frame["label"], fill="#e0e8ea", font=small, anchor="rt")
    nodes = {n["id"]: n for n in frame["nodes"]}
    for edge in frame["edges"]:
        a, b = nodes[edge["source"]], nodes[edge["target"]]
        opacity = min(a.get("opacity", 1), b.get("opacity", 1))
        draw.line((a["x"], a["y"], b["x"], b["y"]), fill=_color("#507c86", opacity), width=1)
    for node in frame["nodes"]:
        x, y, radius = node["x"], node["y"], node["r"]
        opacity = node.get("opacity", 1)
        if radius < 1 or opacity <= 0:
            continue
        color = COLORS[node["kind"]]
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=_color(color, opacity * 0.2),
            outline=_color(color, opacity),
            width=2,
        )
        if radius > 10:
            label = "Token launch" if node["id"] == "launch" else node["address"][:4]
            draw.text((x, y - 5), label, fill=_color("#e5edef", opacity), font=small, anchor="mm")
        if node["id"] == "launch":
            label = "Recorded on chain" if payload["launch"] else "Evidence pending"
            draw.text((x, y + 14), label, fill="#bfcacb", font=small, anchor="mm")
    draw.line((22, 550, 778, 550), fill="#35424a", width=3)
    draw.line((22, 550, 22 + progress * 756, 550), fill=COLORS["launch"], width=3)
    draw.text((22, 568), "Saved evidence · visual replay", fill="#afbec4", font=small)
    draw.text((778, 568), "Replay " + payload["id"][:16], fill="#afbec4", font=small, anchor="rt")
    return image


def render_gif(payload: dict) -> bytes:
    if not verify_replay(payload):
        raise ValueError("Replay quality checks failed")
    frames, durations = [], []

    def append(frame: dict, duration: int, progress: float) -> None:
        # Palette frames keep peak memory bounded even for twelve keyframes.
        image = draw_frame(payload, frame, progress)
        frames.append(image.quantize(colors=64, method=Image.Quantize.FASTOCTREE))
        durations.append(duration)
        image.close()

    keys = payload["frames"]
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
