"""Share cards and link previews.

A link to a ticker, coin, call or report unfurls as its own card rather than
the site's generic blurb. These functions draw those PNGs and write the
og: text beside them. The routes that serve them stay in main.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import textwrap
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

from runner_web.memecoin_chain_parser import short_address

EASTERN = ZoneInfo("America/New_York")


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


CARD_GLYPHS = str.maketrans(
    {"\u2013": "-", "\u2014": "-", "\u00d7": "x", "\u2019": "'", "\u201c": '"', "\u201d": '"'}
)


def _card_text(value: Any) -> str:

    return str(value).translate(CARD_GLYPHS)


def ticker_share(detail: dict[str, Any]) -> dict[str, Any]:
    """What a shared ticker link should say about itself.

    A link to a ticker used to unfurl with the site's generic blurb, so every
    alert and every link anyone pasted looked identical. This gives the page its
    own identity: the symbol, what it is doing, and what the scanner makes of it.
    """

    ticker = str(detail.get("ticker") or "")
    current = detail.get("current") or {}
    gate = detail.get("evidence_gate") or {}
    company = str(detail.get("company") or "").strip()
    change = _number(current.get("change_pct"))
    price = _card_price(current.get("price"))
    move = f"{change:+.1f}%" if change is not None else None
    headline = " · ".join(part for part in (f"${ticker}", price, move) if part)
    summary_parts = [part for part in (company, gate.get("summary")) if part]
    signals = [str(item) for item in (current.get("signals") or [])[:2]]
    if not summary_parts and signals:
        summary_parts.append(" · ".join(signals))
    summary = " · ".join(summary_parts) or "Scanner coverage and source evidence."
    version = hashlib.sha256(
        "|".join(
            str(value)
            for value in (
                current.get("price"),
                current.get("change_pct"),
                current.get("trade_state"),
                current.get("quote_time"),
            )
        ).encode()
    ).hexdigest()[:10]
    return {
        "title": headline,
        "summary": summary[:200],
        "path": f"/stock/{ticker}",
        "card_path": f"/stock/{ticker}/card.png?v={version}",
    }


def _coin_share_address(coin: dict[str, Any]) -> str:
    """The contract address, only when it is shaped like one, as the page shows it."""

    address = str(coin.get("token_address") or "")
    return address if COIN_ADDRESS_RE.fullmatch(address) else ""


def _coin_amount(coin: dict[str, Any], key: str) -> str:
    """A coin's amount label, or nothing when the market has not reported it."""

    label = str(coin.get(f"{key}_label") or "")
    return "" if label in {"", "—", "-"} else label


COIN_ADDRESS_RE = re.compile(r"(?:[1-9A-HJ-NP-Za-km-z]{32,44}|0x[a-fA-F0-9]{40})")


def memecoin_share(detail: dict[str, Any], coin_id: str) -> dict[str, Any]:
    """What a shared coin link should say about itself.

    A launch can copy a famous coin's name, so a chain coin unfurls under its
    contract address. The creator's name is left off the card entirely.
    """

    coin = detail.get("coin") or {}
    address = _coin_share_address(coin)
    label = short_address(address) if address else f"${coin.get('symbol') or ''}"
    change = _number(coin.get("change_24h"))
    move = f"{change:+.1f}% 24h" if change is not None else None
    headline = " · ".join(part for part in (label, coin.get("price_label"), move) if part)
    facts = [
        f"Volume {_coin_amount(coin, 'volume')}" if _coin_amount(coin, "volume") else "",
        f"Market cap {_coin_amount(coin, 'market_cap')}"
        if _coin_amount(coin, "market_cap")
        else "",
    ]
    flags = [str(item.get("title")) for item in coin.get("findings") or [] if item.get("title")]
    summary = " · ".join(part for part in (address, *facts, *flags[:1]) if part)
    version = hashlib.sha256(
        "|".join(str(coin.get(key)) for key in ("price", "change_24h", "observed_at")).encode()
    ).hexdigest()[:10]
    return {
        "title": headline,
        "summary": (summary or "Memecoin evidence and price history.")[:200],
        "path": f"/memecoins/coin/{coin_id}",
        "card_path": f"/memecoins/coin/{coin_id}/card.png?v={version}",
    }


def _memecoin_card_png(detail: dict[str, Any]) -> bytes:
    coin = detail.get("coin") or {}
    address = _coin_share_address(coin)
    change = _number(coin.get("change_24h"))
    image = Image.new("RGB", (1200, 630), "#090b0b")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (55, 55, 1145, 575), radius=34, fill="#111514", outline="#57e389", width=3
    )
    draw.text((95, 84), _card_text("RATi RUNNERS · MEMECOIN"), "#87e8a9", font=font(26, True))
    label = short_address(address) if address else f"${coin.get('symbol') or ''}"
    draw.text((95, CARD_PRICE_Y), _card_text(label), "#f4f8f6", font=font(64, True))
    if address:
        # The full address under the short one: it is the coin's identity.
        draw.text((95, CARD_COMPANY_Y), address, "#7e8b86", font=font(20))
    price = _card_price(coin.get("price")) or "No price"
    draw.text((1105, CARD_PRICE_Y), price, "#f4f8f6", font=font(60, True), anchor="ra")
    move = f"{change:+.1f}% 24h" if change is not None else "—"
    tone = "#87e8a9" if (change or 0) > 0 else "#f2a3ac" if (change or 0) < 0 else "#9fb2a8"
    draw.text((1105, CARD_CHANGE_Y), move, tone, font=font(32, True), anchor="ra")
    draw.line((95, 292, 1105, 292), fill="#26302c", width=2)
    points = [
        {"time": row.get("observed_at"), "close": row.get("price")}
        for row in detail.get("history") or []
    ]
    _draw_card_chart(draw, {"points": points}, (95, 312, 690, 500))
    facts = [
        ("VOLUME 24H", _coin_amount(coin, "volume")),
        ("MARKET CAP", _coin_amount(coin, "market_cap")),
        ("NETWORK", str(coin.get("network") or "").upper() or None),
    ]
    top = 312
    for caption, value in facts:
        if not value:
            continue
        draw.text((740, top), caption, "#65716b", font=font(18, True))
        draw.text((740, top + 24), _card_text(value), "#f4f8f6", font=font(30, True))
        top += 64
    findings = [str(item.get("title")) for item in coin.get("findings") or [] if item.get("title")]
    if findings:
        draw.text(
            (740, top), _card_text(f"Flag: {findings[0]}")[:36], "#f2a3ac", font=font(20, True)
        )
    draw.text(
        (95, CARD_FOOTER_Y),
        _card_text("runners.rati.chat · Research only"),
        "#65716b",
        font=font(20),
    )
    stamp = _card_time(coin.get("observed_at"))
    if stamp:
        draw.text((1105, CARD_FOOTER_Y), stamp, "#65716b", font=font(20), anchor="ra")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# Tones shared with the site's charts and tags, so a card reads like the page.
CARD_TONES = {
    "running": "#a5e5b9",
    "setup": "#73ceff",
    "extended": "#ffad70",
    "avoid": "#ef99a4",
    "watch": "#c4a7ef",
    "paused": "#96a49b",
    "up": "#a5e5b9",
    "down": "#ef99a4",
    "neutral": "#96a49b",
}


CARD_DRIVERS = {
    "market": "#73ceff",
    "sec_event": "#c4a7ef",
    "news": "#a5e5b9",
    "social_search": "#ffad70",
    "community": "#f5c66b",
}


def _card_chart_rows(chart: dict[str, Any]) -> list[tuple[float, float]]:
    rows = []
    for point in chart.get("points") or []:
        try:
            stamp = datetime.fromisoformat(str(point.get("time") or "").replace("Z", "+00:00"))
            value = float(point.get("close"))
        except (TypeError, ValueError):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        rows.append((stamp.timestamp(), value))
    return sorted(rows)


def _card_tone_at(states: list[dict[str, Any]], moment: float) -> str:
    tone = ""
    for change in states:
        try:
            at = datetime.fromisoformat(str(change.get("time") or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        if at.timestamp() > moment:
            break
        tone = str(change.get("tone") or "")
    return tone


def _draw_card_chart(
    draw: Any,
    chart: dict[str, Any],
    box: tuple[int, int, int, int],
) -> list[str]:
    x0, y0, x1, y1 = box
    rows = _card_chart_rows(chart)
    if len(rows) < 2:
        draw.text((x0, (y0 + y1) // 2 - 10), "Price history pending", "#65716b", font=font(22))
        return []
    values = [value for _, value in rows]
    low, high = min(values), max(values)
    span = high - low or 1.0
    start, end = rows[0][0], rows[-1][0]
    timeline = end - start or 1.0
    states = list(chart.get("states") or [])

    def px(moment: float) -> float:
        return x0 + (moment - start) / timeline * (x1 - x0)

    def py(value: float) -> float:
        return y1 - (value - low) / span * (y1 - y0)

    coords = [(px(moment), py(value)) for moment, value in rows]
    draw.polygon([*coords, (x1, y1), (x0, y1)], fill="#17291e")

    tones: list[str] = []
    run: list[tuple[float, float]] = []
    run_tone = _card_tone_at(states, rows[0][0])
    for (moment, _value), point in zip(rows, coords, strict=True):
        tone = _card_tone_at(states, moment)
        if tone != run_tone and run:
            draw.line(run, fill=CARD_TONES.get(run_tone, "#96a49b"), width=3, joint="curve")
            if run_tone and run_tone not in tones:
                tones.append(run_tone)
            run = [run[-1]]
            run_tone = tone
        run.append(point)
    if run:
        draw.line(run, fill=CARD_TONES.get(run_tone, "#96a49b"), width=3, joint="curve")
        if run_tone and run_tone not in tones:
            tones.append(run_tone)
    return tones


def _draw_card_legend(draw: Any, tones: list[str], x: int, y: int) -> None:
    label_font = font(19, True)
    cursor = x
    for tone in tones:
        draw.ellipse((cursor, y + 4, cursor + 13, y + 17), fill=CARD_TONES.get(tone, "#96a49b"))
        text = tone.upper()
        draw.text((cursor + 20, y), text, "#9fb2a8", font=label_font)
        cursor += 30 + int(draw.textlength(text, font=label_font)) + 26


def _draw_card_ring(draw: Any, cx: int, cy: int, radius: int, detail: dict[str, Any]) -> None:
    current = detail.get("current") or {}
    score_detail = current.get("score_detail") or {}
    parts: list[tuple[float, str]] = []
    for driver in score_detail.get("drivers") or []:
        value = _number(driver.get("value")) or 0.0
        if value > 0:
            parts.append((value, CARD_DRIVERS.get(str(driver.get("key")), "#73ceff")))
    for penalty in score_detail.get("penalties") or []:
        value = _number(penalty.get("value")) or 0.0
        if value:
            parts.append((abs(value), "#ef99a4"))
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    width = max(10, radius // 4)
    draw.ellipse(box, outline="#26302c", width=width)
    total = sum(value for value, _ in parts)
    if total:
        angle = -90.0
        for value, color in parts:
            sweep = value / total * 360.0
            draw.arc(box, angle, angle + sweep, fill=color, width=width)
            angle += sweep
    inner = radius - width // 2 - 4
    draw.ellipse(
        (cx - inner, cy - inner, cx + inner, cy + inner),
        fill="#253e2d",
        outline="#668770",
    )
    ticker = _card_text(detail.get("ticker") or "")
    score = current.get("score")
    draw.text((cx, cy - 12), ticker, "#f4f8f6", font=font(20, True), anchor="mm")
    if score is not None:
        draw.text((cx, cy + 14), f"{float(score):.0f}", "#f4f8f6", font=font(22, True), anchor="mm")


def _draw_card_map(draw: Any, map_data: dict[str, Any], box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    people: dict[str, dict[str, Any]] = {}
    for event in map_data.get("events") or []:
        for person in event.get("people") or []:
            key = str(person.get("id") or person.get("name") or "")
            if key and key not in people:
                people[key] = {
                    "name": str(person.get("name") or "Reporting person"),
                    "tone": str(event.get("tone") or "neutral"),
                }
    chosen = list(people.values())[:4]
    radius = 20
    name_font = font(16, True)
    for index, person in enumerate(chosen):
        dx = -1 if index % 2 == 0 else 1
        dy = -1 if index < 2 else 1
        px = cx + dx * (radius + 118)
        py = cy + dy * (radius + 58)
        draw.line((cx, cy, px, py), fill="#42584b", width=2)
        tone = CARD_TONES.get(person["tone"], "#789681")
        draw.ellipse(
            (px - radius, py - radius, px + radius, py + radius),
            fill="#202e26",
            outline=tone,
            width=2,
        )
        initials = "".join(part[:1] for part in person["name"].split()[:2]).upper() or "?"
        draw.text((px, py), initials, "#f4f8f6", font=font(18, True), anchor="mm")
        name = _card_text(person["name"])
        if len(name) > 20:
            name = name[:19].rstrip(" .") + "…"
        draw.text((px, py + radius + 8), name, "#9fb2a8", font=name_font, anchor="ma")


# Card rows, shared by the price block and its badge so they cannot collide.
CARD_PRICE_Y = 138


CARD_COMPANY_Y = 232


CARD_CHANGE_Y = 214


# The badge must stay above this row so it never reaches the divider at y 292.
CARD_DATE_Y = 262


CARD_BADGE_HEIGHT = 44


CARD_FOOTER_Y = 548


def _ticker_card_png(
    detail: dict[str, Any],
    chart: dict[str, Any] | None = None,
    map_data: dict[str, Any] | None = None,
) -> bytes:

    current = detail.get("current") or {}
    ticker = str(detail.get("ticker") or "")
    change = _number(current.get("change_pct"))
    image = Image.new("RGB", (1200, 630), "#090b0b")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (55, 55, 1145, 575), radius=34, fill="#111514", outline="#57e389", width=3
    )
    draw.text((95, 84), _card_text("RATi RUNNERS · TICKER"), "#87e8a9", font=font(26, True))

    draw.text((95, CARD_PRICE_Y), _card_text(f"${ticker}"), "#f4f8f6", font=font(76, True))
    company = _card_text(detail.get("company") or "")[:40]
    if company:
        # The name sits under the symbol it belongs to, not over the price.
        draw.text((95, CARD_COMPANY_Y), company, "#7e8b86", font=font(24))
    price = _card_price(current.get("price")) or "No price"
    draw.text((1105, CARD_PRICE_Y), price, "#f4f8f6", font=font(60, True), anchor="ra")
    move = f"{change:+.1f}%" if change is not None else "—"
    tone = "#87e8a9" if (change or 0) > 0 else "#f2a3ac" if (change or 0) < 0 else "#9fb2a8"
    change_font = font(32, True)
    draw.text((1105, CARD_CHANGE_Y), move, tone, font=change_font, anchor="ra")
    stamp = _card_time(current.get("quote_time") or current.get("event_at"))
    # The state badge shares the change row, to the left of the move, rather than
    # landing on the line below it.
    badge_right = 1105 - int(draw.textlength(move, font=change_font)) - 18
    _draw_ticker_badge(draw, current, right=badge_right, top=CARD_CHANGE_Y)

    draw.line((95, 292, 1105, 292), fill="#26302c", width=2)

    tones = _draw_card_chart(draw, chart or {}, (95, 312, 690, 500))
    _draw_card_legend(draw, tones or ["watch"], 95, 516)
    _draw_card_map(draw, map_data or {"events": []}, (720, 300, 1105, 520))
    _draw_card_ring(draw, 912, 410, 46, detail)

    draw.text(
        (95, CARD_FOOTER_Y),
        _card_text("runners.rati.chat · Research only"),
        "#65716b",
        font=font(20),
    )
    if stamp:
        # When the price is from, right-aligned along the bottom edge.
        draw.text((1105, CARD_FOOTER_Y), stamp, "#65716b", font=font(20), anchor="ra")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _tone_word(value: float | None) -> str:
    if value is None:
        return "flat"
    return "up" if value > 0 else "down" if value < 0 else "flat"


def _draw_ticker_badge(
    draw: Any, current: dict[str, Any], *, right: int, top: int
) -> tuple[int, int, int, int] | None:
    """The trade state, drawn clear of the price block's own lines."""

    state = str(current.get("trade_state") or "").upper()
    level = str(current.get("rug_level") or "").lower()
    if level in {"high", "critical"}:
        label, fill, ink = "RISK FACTORS DETECTED", "#331a1e", "#f2a3ac"
    elif state in {"AVOID", "EXIT"}:
        label, fill, ink = state, "#331a1e", "#f2a3ac"
    elif state and state != "UNKNOWN":
        label, fill, ink = state, "#123021", "#87e8a9"
    else:
        return None
    badge = font(22, True)
    width = int(draw.textlength(label, font=badge)) + 36
    box = (right - width, top, right, top + CARD_BADGE_HEIGHT)
    draw.rounded_rectangle(box, radius=12, fill=fill)
    draw.text((right - width + 18, top + 10), label, ink, font=badge)
    return box


def _market_report_card_png(report: dict[str, Any]) -> bytes:

    is_post = report["report_type"] == "post_market"
    pick = (report.get("share") or {}).get("top_pick")
    accent = "#e5ba7b" if is_post else "#87b6f7"
    image = Image.new("RGB", (1200, 630), "#090b0b")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((55, 55, 1145, 575), radius=34, fill="#111514", outline=accent, width=3)
    draw.text(
        (95, 88),
        _card_text(f"RATi RUNNERS · {str(report['label']).upper()}"),
        accent,
        font=font(27, True),
    )
    day_label = _card_text(f"{report['report_day']} · {report['as_of_label']}")
    draw.text(
        (1105 - draw.textlength(day_label, font=font(23)), 92),
        day_label,
        "#7e8b86",
        font=font(23),
    )

    pick_label = "COMPANY IN FOCUS" if pick and pick.get("editorial") else "WATCH LEADER"
    narrative = report.get("narrative") or {}
    if narrative.get("stories"):
        draw.text((95, 152), "THE SESSION STORY", "#9fb2a8", font=font(21, True))
        headline_lines = textwrap.wrap(_card_text(narrative["headline"]), width=38)[:3]
        draw.multiline_text(
            (95, 187), "\n".join(headline_lines), fill="#f4f8f6", font=font(46, True), spacing=6
        )
    elif pick:
        draw.text((95, 152), pick_label, "#9fb2a8", font=font(21, True))
        draw.text((95, 182), f"${pick['ticker']}", "#f4f8f6", font=font(76, True))
        _draw_pick_verdict(draw, pick, is_post)
        draw.text((95, 282), _card_text(_pick_line(pick, is_post)), "#cfe0d7", font=font(28))
    else:
        draw.text((95, 182), _card_text(report["headline"])[:28], "#f4f8f6", font=font(58, True))

    analysis = report.get("analysis") or {}
    lead = _card_text(
        narrative["intro"]
        if narrative.get("stories")
        else pick["reason"]
        if pick and pick.get("editorial")
        else analysis.get("headline") or report["summary"]
    )
    lines = textwrap.wrap(lead, width=62)[:2]
    if len(textwrap.wrap(lead, width=62)) > 2:
        lines[-1] = lines[-1].rstrip(" .") + "…"
    draw.multiline_text((95, 340), "\n".join(lines), fill="#9fb2a8", font=font(26), spacing=10)

    cards = report["record_cards"] if is_post else report["metric_cards"]
    _draw_scorecard(draw, cards)
    draw.text((95, 543), _card_text("runners.rati.chat · Research only"), "#65716b", font=font(21))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _pick_line(pick: dict[str, Any], is_post: bool) -> str:
    if pick.get("editorial"):
        price = _card_price(pick.get("price")) or "Price unavailable"
        move = pick.get("change_pct")
        move_label = f"{float(move):+.1f}%" if move is not None else "Move unavailable"
        return f"{price} · {move_label} · Saved scan research"
    reference = _card_price(pick.get("reference_price"))
    target = _card_price(pick.get("target_price"))
    if not target:
        return "No directional target · the saved risk state called for a pass"
    if not is_post:
        way = "up from" if pick.get("direction") == "up" else "down from"
        return f"Target {target} by the close · {way} {reference or 'the open'}"
    close = _card_price(pick.get("close_price"))
    move = pick.get("session_return_pct")
    tail = f" · {float(move):+.1f}% on the day" if move is not None else ""
    return f"Target {target} · closed {close or 'unsettled'}{tail}"


def _card_time(value: Any) -> str:
    """The quote time in US Eastern, the clock the market actually runs on."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _card_text(raw[:16])
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    eastern = moment.astimezone(EASTERN)
    zone = "EDT" if eastern.dst() else "EST"
    return _card_text(f"{eastern.strftime('%b %d, %Y')} · {eastern.strftime('%H:%M')} {zone}")


def _card_price(value: Any) -> str | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    text = f"{price:.4f}".rstrip("0").rstrip(".")
    return f"${text or '0'}"


def _draw_pick_verdict(draw: Any, pick: dict[str, Any], is_post: bool) -> None:
    status = str(pick.get("status") or "")
    tones = {
        "hit": ("HIT", "#123021", "#87e8a9"),
        "miss": ("MISS", "#331a1e", "#f2a3ac"),
        "pass": ("PASS", "#1c2220", "#9fb2a8"),
        "review": ("REVIEW", "#1c2220", "#9fb2a8"),
    }
    if is_post and status in tones:
        label, fill, ink = tones[status]
    elif not is_post and pick.get("direction") in {"up", "down"}:
        up = pick["direction"] == "up"
        label = "TARGET UP" if up else "TARGET DOWN"
        fill, ink = ("#123021", "#87e8a9") if up else ("#331a1e", "#f2a3ac")
    else:
        return
    badge = font(27, True)
    width = draw.textlength(label, font=badge) + 46
    draw.rounded_rectangle((1105 - width, 196, 1105, 252), radius=14, fill=fill)
    draw.text((1105 - width + 23, 208), label, ink, font=badge)


def _draw_scorecard(draw: Any, cards: list[dict[str, Any]]) -> None:
    if not cards:
        return
    left, right, top, bottom = 95, 1105, 410, 512
    width = (right - left) / len(cards)
    draw.rounded_rectangle((left, top, right, bottom), radius=16, outline="#26302c", width=2)
    for index, card in enumerate(cards[:4]):
        x = left + width * index
        if index:
            draw.line((x, top + 14, x, bottom - 14), fill="#26302c", width=2)
        tone = str(card.get("tone") or "")
        ink = {"up": "#87e8a9", "down": "#f2a3ac"}.get(tone, "#f4f8f6")
        value = _card_text(card["value"])
        label = _card_text(card["label"]).upper()
        value_font, label_font = font(38, True), font(19, True)
        for label_size in range(18, 12, -1):
            if draw.textlength(label, font=label_font) <= width - 24:
                break
            label_font = font(label_size, True)
        draw.text(
            (x + (width - draw.textlength(value, font=value_font)) / 2, top + 16),
            value,
            ink,
            font=value_font,
        )
        draw.text(
            (x + (width - draw.textlength(label, font=label_font)) / 2, top + 66),
            label,
            "#7e8b86",
            font=label_font,
        )


def call_share(call: dict[str, Any], detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """What a shared Call link should say about itself.

    A Call is the product's smallest social object, so a pasted link has to
    carry the caller, the subject and the result on its own.
    """

    public_id = str(call.get("public_id") or "")
    ticker = str(call.get("ticker") or "")
    handle = str(call.get("caller_handle") or "A caller")
    entry = _card_price(call.get("entry_price"))
    return_pct = call.get("return_pct")
    move = f"{float(return_pct):+.1f}%" if return_pct is not None else None
    company = str((detail or {}).get("company") or "").strip()
    if str(call.get("status")) == "closed":
        title = f"{handle}'s ${ticker} Call closed {move}" if move else f"{handle}'s ${ticker} Call"
        summary = " · ".join(
            part
            for part in (company, f"Entry {entry}" if entry else None, "Paper Call settled")
            if part
        )
    else:
        title = f"{handle} just called ${ticker}"
        summary = " · ".join(
            part
            for part in (
                company,
                f"Entry {entry}" if entry else None,
                f"{move} so far" if move else "Open paper Call",
            )
            if part
        )
    version = hashlib.sha256(
        "|".join(
            str(value)
            for value in (
                call.get("entry_price"),
                call.get("exit_price"),
                call.get("status"),
                call.get("updated_at"),
            )
        ).encode()
    ).hexdigest()[:10]
    return {
        "title": title,
        "summary": (summary or "A public paper Call on RATi Runners.")[:200],
        "path": f"/c/{public_id}",
        "card_path": f"/c/{public_id}/card.png?v={version}",
    }


def _call_card_png(call: dict[str, Any], detail: dict[str, Any] | None = None) -> bytes:
    ticker = str(call.get("ticker") or "")
    handle = _card_text(str(call.get("caller_handle") or "RATi runner"))
    closed = str(call.get("status")) == "closed"
    entry = _card_price(call.get("entry_price"))
    mark = _card_price(call.get("mark_price"))
    return_pct = call.get("return_pct")
    move = f"{float(return_pct):+.1f}%" if return_pct is not None else "—"
    tone = (
        "#87e8a9"
        if return_pct is not None and float(return_pct) > 0
        else "#f2a3ac"
        if return_pct is not None and float(return_pct) < 0
        else "#9fb2a8"
    )
    image = Image.new("RGB", (1200, 630), "#090b0b")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (55, 55, 1145, 575), radius=34, fill="#111514", outline="#57e389", width=3
    )
    draw.text((95, 88), _card_text("RATI RUNNERS · CALL"), "#87e8a9", font=font(27, True))
    status_label = "SETTLED" if closed else "OPEN"
    status_ink = "#9fb2a8" if closed else "#87e8a9"
    badge = font(23, True)
    width = draw.textlength(status_label, font=badge) + 40
    draw.rounded_rectangle((1105 - width, 84, 1105, 132), radius=14, fill="#1c2220")
    draw.text((1105 - width + 20, 96), status_label, status_ink, font=badge)

    draw.text((95, 168), _card_text(f"${ticker}"), "#f4f8f6", font=font(84, True))
    company = _card_text(str((detail or {}).get("company") or ""))[:42]
    if company:
        draw.text((95, 272), company, "#7e8b86", font=font(27))

    verb = "called" if closed else "is calling"
    draw.text(
        (95, 348),
        f"{handle} {verb} ${_card_text(ticker)} up",
        "#cfe0d7",
        font=font(40, True),
    )
    facts = [
        ("Entry", entry or "—"),
        ("Now" if not closed else "Close", mark or "—"),
        ("Return", move),
        ("Earned", f"{int(call.get('flash_reward') or 0)} Flash" if closed else "At settlement"),
    ]
    left, right, top = 95, 1105, 430
    step = (right - left) / len(facts)
    for index, (label, value) in enumerate(facts):
        x = left + step * index
        ink = tone if label == "Return" else "#f4f8f6"
        value_font, label_font = font(34, True), font(19, True)
        draw.text((x, top + 12), _card_text(value), ink, font=value_font)
        draw.text((x, top + 62), label.upper(), "#7e8b86", font=label_font)

    draw.text((95, 543), _card_text("runners.rati.chat · Research only"), "#65716b", font=font(21))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default(size=size)
