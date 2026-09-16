"""Telegram delivery for public channel posts.

The room gets one batched update announcement when enough has landed: new
runners, a frozen pre-market or post-market report, and a research report that
went public are gathered into the same message rather than posted one at a time.
A new build can also announce itself once. Formatting and the batch rule live
here so they can be tested without a database; the worker in ``main`` loads the
rows, writes the announcement, and records the outcome.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS = 10
DEFAULT_MIN_SCORE = 37.0
DEFAULT_MAX_PER_RUN = 10
DEFAULT_MAX_REPORTS_PER_RUN = 5
DEFAULT_ANNOUNCE_BATCH_MIN = 2
DEFAULT_ANNOUNCE_DEBOUNCE_MINUTES = 30
MAX_MESSAGE_CHARS = 4096
MARKET_REPORT_LEADER_LIMIT = 3
RELEASE_NOTES_LIMIT = 900

_TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    """Resolved Telegram alert settings for one dispatch."""

    bot_token: str
    chat_id: str
    min_score: float = DEFAULT_MIN_SCORE
    max_per_run: int = DEFAULT_MAX_PER_RUN

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def endpoint(self) -> str:
        return f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"


def alerts_enabled(value: str | None = None) -> bool:
    """Report whether the feature flag is on. Defaults to off."""

    raw = os.getenv("TELEGRAM_RUNNER_ALERTS", "0") if value is None else value
    return str(raw).strip().lower() in _TRUE_VALUES


def memecoin_alerts_enabled() -> bool:
    """The integration team enables new-token GIF delivery explicitly."""
    return alerts_enabled(os.getenv("TELEGRAM_MEMECOIN_ALERTS", "0"))


class AnimationDeliveryError(RuntimeError):
    def __init__(self, status: str, *, retry_after: int = 60):
        super().__init__("Telegram animation " + status)
        self.status = status
        self.retry_after = max(30, min(retry_after, 86400))


def send_animation(
    config: TelegramConfig,
    gif: bytes,
    caption: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> int:
    """Upload one GIF and caption after the caller enables the media feature."""
    if not memecoin_alerts_enabled() or not config.configured:
        raise AnimationDeliveryError("failed")
    if not gif.startswith((b"GIF87a", b"GIF89a")) or len(gif) > 8 * 1024 * 1024:
        raise AnimationDeliveryError("failed")
    boundary = "rati-" + secrets.token_hex(16)
    caption = caption.encode("utf-16-le")[:2048].decode("utf-16-le", errors="ignore")
    trailing = len(caption) - len(caption.rstrip(chr(92)))
    if trailing % 2:
        caption = caption[:-1]
    fields = {
        "chat_id": config.chat_id,
        "caption": caption,
        "parse_mode": "MarkdownV2",
        "disable_notification": "false",
    }
    parts = []
    for name, value in fields.items():
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                + value
                + "\r\n"
            ).encode()
        )
    parts.extend(
        [
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="animation"; '
                'filename="token-replay.gif"\r\nContent-Type: image/gif\r\n\r\n'
            ).encode(),
            gif,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    request = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/bot{config.bot_token}/sendAnimation",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with opener(request, timeout=30) as response:
            body = json.loads(response.read(64 * 1024))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read(64 * 1024))
        except (ValueError, OSError):
            body = {}
        finally:
            exc.close()
        if exc.code == 429:
            delay = (body.get("parameters") or {}).get("retry_after", 60)
            raise AnimationDeliveryError("retry", retry_after=int(delay)) from None
        raise AnimationDeliveryError("failed" if 400 <= exc.code < 500 else "uncertain") from None
    except (OSError, ValueError):
        raise AnimationDeliveryError("uncertain") from None
    if not isinstance(body, dict):
        raise AnimationDeliveryError("uncertain")
    if body.get("ok") is False:
        if body.get("error_code") == 429:
            raise AnimationDeliveryError(
                "retry", retry_after=int((body.get("parameters") or {}).get("retry_after", 60))
            )
        raise AnimationDeliveryError("failed")
    message_id = (body.get("result") or {}).get("message_id")
    if body.get("ok") is not True or type(message_id) is not int or message_id <= 0:
        raise AnimationDeliveryError("uncertain")
    return message_id


def release_announcements_enabled(value: str | None = None) -> bool:
    """Report whether build-change announcements to the chat are on."""

    raw = os.getenv("TELEGRAM_RELEASE_ANNOUNCEMENTS", "0") if value is None else value
    return str(raw).strip().lower() in _TRUE_VALUES


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        LOG.warning("Ignoring invalid %s=%r", name, raw)
        return default


def _int_env(name: str, default: int) -> int:
    return max(1, int(_float_env(name, float(default))))


def bot_token_from_env() -> str:
    """Read the bot token. TELEGRAM_API_TOKEN is the canonical name; the
    older TELEGRAM_BOT_TOKEN is still accepted."""

    return (
        os.getenv("TELEGRAM_API_TOKEN", "").strip() or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    )


def config_from_env() -> TelegramConfig:
    """Read alert settings from the environment at call time."""

    return TelegramConfig(
        bot_token=bot_token_from_env(),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        min_score=_float_env("TELEGRAM_MIN_SCORE", DEFAULT_MIN_SCORE),
        max_per_run=_int_env("TELEGRAM_MAX_PER_RUN", DEFAULT_MAX_PER_RUN),
    )


def _score(entry: Mapping[str, Any]) -> float:
    value = entry.get("score")
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def select_new_runners(
    entries: Iterable[Mapping[str, Any]],
    *,
    min_score: float = DEFAULT_MIN_SCORE,
    limit: int = DEFAULT_MAX_PER_RUN,
) -> list[dict[str, Any]]:
    """Return the highest-scoring entries that clear the score floor."""

    eligible = [dict(entry) for entry in entries if _score(entry) >= min_score]
    eligible.sort(
        key=lambda entry: (_score(entry), str(entry.get("entered_at") or "")),
        reverse=True,
    )
    return eligible[: max(0, limit)]


def announcement_batch_ready(
    count: int,
    oldest_age_minutes: float,
    *,
    min_items: int = DEFAULT_ANNOUNCE_BATCH_MIN,
    debounce_minutes: int = DEFAULT_ANNOUNCE_DEBOUNCE_MINUTES,
) -> bool:
    """Decide whether enough has piled up to announce it.

    A batch goes out as soon as it reaches the floor. A lone item waits for more
    to arrive, so a single runner does not cost a message, but it is not stranded
    either: once it is older than the debounce window it goes out on its own.
    """

    if count <= 0:
        return False
    if count >= max(1, min_items):
        return True
    return oldest_age_minutes >= max(0, debounce_minutes)


def _api_call(
    config: TelegramConfig,
    method: str,
    payload: dict[str, Any],
    *,
    opener: Callable[..., Any] | None = None,
) -> Any:
    """Call one Bot API method.

    The endpoint carries the bot token, so nothing here puts the URL into an error
    or a log line. Callers get the method name and the status, which is enough to
    diagnose a failure without leaking the credential.
    """

    if not config.configured:
        raise RuntimeError("Telegram bot token and chat id are required")
    if opener is None:
        opener = urllib.request.urlopen
    request = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/bot{config.bot_token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=SEND_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            body = response.read()
            if status >= 400:
                details = (
                    body.decode("utf-8", errors="ignore")
                    if isinstance(body, bytes)
                    else str(body)
                )
                raise RuntimeError(
                    f"Telegram {method} failed with status {status}: {details[:200]}"
                )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(64 * 1024)
            details = (
                body.decode("utf-8", errors="ignore") if isinstance(body, bytes) else str(body)
            )
        finally:
            exc.close()
        raise RuntimeError(
            f"Telegram {method} failed with status {exc.code}: {details[:200]}"
        ) from exc
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return {}


def send_reply(
    config: TelegramConfig,
    chat_id: int,
    text: str,
    *,
    reply_to_message_id: int | None = None,
    opener: Callable[..., Any] | None = None,
) -> Any:
    """Reply in a chat, threaded onto the message being answered when given."""

    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:MAX_MESSAGE_CHARS],
        "disable_notification": True,
    }
    if reply_to_message_id:
        payload["reply_parameters"] = {
            "message_id": reply_to_message_id,
            "allow_sending_without_reply": True,
        }
    return _api_call(config, "sendMessage", payload, opener=opener)


def set_reaction(
    config: TelegramConfig,
    chat_id: int,
    message_id: int,
    emoji: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> Any:
    """React to a message instead of speaking over the room."""

    return _api_call(
        config,
        "setMessageReaction",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
            "is_big": False,
        },
        opener=opener,
    )


# ---------------------------------------------------------------------------
# Markdown V2 formatters and link-preview sender.
#
# The formatters below produce Telegram Markdown V2 so the channel reads
# like a message board: bold tickers, emoji-coded state, an inline metrics
# line per runner, and exactly one URL per block so Telegram unfurls a
# link preview from the entity's ticker page. Every user-provided string
# is escaped with escape_markdown_v2, and every URL sits on its own line.
# Telegram parse failures fall back to plain text so nothing is dropped
# silently.
# ---------------------------------------------------------------------------

# Telegram reserves all of these in Markdown V2 body text wherever they appear.
# There is no line-start exemption: the period in "12.5%" and the hyphen in
# "8-K" fail the parse exactly like one in the first column would.
_MD_V2_SPECIAL = "_*[]()~`>#+-=|{}.!" + chr(92)
_ESCAPE_RE = re.compile("([" + re.escape(_MD_V2_SPECIAL) + "])")
_UNESCAPE_RE = re.compile(r"\\([" + re.escape(_MD_V2_SPECIAL) + "])")
_MARKER_RE = re.compile(r"(?<!\\)[*_~`]")
_LINK_RE = re.compile(r"\[((?:[^\[\]\\]|\\.)*)\]\(((?:[^()\\]|\\.)*)\)")


def escape_markdown_v2(text):
    """Escape every character Telegram reserves in Markdown V2 body text.

    Escaping one that did not strictly need it renders identically; missing one
    costs the whole message, because Telegram rejects the parse and the room
    gets the markup source instead of the card.
    """

    if not text:
        return ""
    return _ESCAPE_RE.sub(r"\\\1", str(text))


def _plain(text):
    """Drop the entity markers, then put the escaped characters back."""

    return _UNESCAPE_RE.sub(r"\1", _MARKER_RE.sub("", text))


def _plain_link(match):
    label = _plain(match.group(1))
    target = _UNESCAPE_RE.sub(r"\1", match.group(2))
    return (label + " " + target).strip()


def strip_markdown_v2(text):
    """Render a Markdown V2 body as the plain text a reader expects.

    Used when Telegram rejects the markup. Resending the source put raw
    asterisks and backslashes in the room; this keeps the words. Links keep
    both halves, since Telegram auto-links a bare URL in a plain message.
    """

    return _plain(_LINK_RE.sub(_plain_link, str(text or "")))


def _first_url(text):
    match = re.search(r"https?://[^\s)]+", text)
    return match.group(0) if match else ""


def markdown_link(label, url):
    """An inline link. A link target reserves only ``\\`` and ``)``."""

    target = str(url).replace("\\", "\\\\").replace(")", "\\)")
    return f"[{escape_markdown_v2(str(label))}]({target})"


def _join_blocks(blocks, limit=MAX_MESSAGE_CHARS):
    """Join rendered blocks without ever splitting one.

    Each block is balanced Markdown V2 on its own. Slicing the joined string at
    a fixed width could strand an opening ``*`` or a trailing backslash, which
    Telegram rejects, so a block that does not fit is dropped whole instead.
    """

    kept: list[str] = []
    used = 0
    for block in blocks:
        if not block:
            continue
        cost = len(block) + (2 if kept else 0)
        if used + cost > limit:
            continue
        kept.append(block)
        used += cost
    return "\n\n".join(kept)


def _truncate_md(text, limit=MAX_MESSAGE_CHARS):
    """Cut an assembled message to the Telegram limit on a block boundary.

    Callers hand us text that already fits; this is the guard for the ones that
    do not. A blind slice can end on a half-written escape, so drop back to the
    last blank line, and failing that trim the dangling backslash.
    """

    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = cut.rfind("\n\n")
    if boundary > 0:
        return cut[:boundary]
    trimmed = cut.rstrip(chr(92))
    return cut[: len(trimmed) + (len(cut) - len(trimmed)) // 2 * 2]


def send_post(config, text, *, preview_url="", parse_mode="MarkdownV2", opener=None):
    """Send one message with a link preview from its first URL.

    Telegram renders a preview for the first URL it finds in the message body.
    To pin which link unfurls, pass preview_url and we surface it as the
    first line. When the Markdown parse fails, the message is resent plain so
    we never drop a notification silently.
    """

    body = _truncate_md(text)
    if preview_url and (preview_url not in body):
        # A bare URL on its own line is not valid Markdown V2 — every dot and
        # hyphen in the host is reserved — so anchor it as an inline link.
        anchor = markdown_link(preview_url, preview_url) if parse_mode else preview_url
        body = _truncate_md(anchor + "\n\n" + body)
    payload = {
        "chat_id": config.chat_id,
        "text": body,
        "disable_notification": False,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    first = preview_url or _first_url(body)
    if first:
        previews = {"is_disabled": False}
        if preview_url:
            previews["url"] = preview_url
        payload["link_preview_options"] = previews
    try:
        return _api_call(config, "sendMessage", payload, opener=opener)
    except RuntimeError as exc:
        if not parse_mode:
            raise
        message = str(exc)
        if "parse" not in message.lower():
            raise
        # Resending the markup source is what put raw asterisks in the room.
        # Strip it to plain text, and say so: this path used to report success.
        LOG.warning("Telegram rejected Markdown V2, resending as plain text: %s", message)
        plain = {
            "chat_id": config.chat_id,
            "text": strip_markdown_v2(body),
            "disable_notification": False,
        }
        if first:
            plain["link_preview_options"] = {"is_disabled": False}
        return _api_call(config, "sendMessage", plain, opener=opener)


def _state_emoji(tag):
    """One emoji that matches the action tag on the list."""

    return {
        "RUNNING":  "\u26A1",
        "SETUP":    "\U0001F535",
        "EXTENDED": "\U0001F7E0",
        "AVOID":    "\U0001F534",
        "WATCH":    "\u26AA",
        "PAUSED":   "\u23F8",
    }.get(str(tag or "").upper(), "")


def _rise_emoji(change):
    try:
        return "\u2B06" if float(change) >= 0 else "\u2B07"
    except (TypeError, ValueError):
        return ""


def _format_metrics_line(entry):
    """The inline metrics run.

    Every number here is escaped before it is placed next to the bold markers.
    A price always renders a decimal point and a move always renders a sign, and
    Telegram reserves both, so an unescaped run fails the whole message.
    """

    parts = []
    price = entry.get("price")
    if price is not None:
        try:
            number = float(price)
        except (TypeError, ValueError):
            pass
        else:
            shown = f"{number:.4f}" if number < 1 else f"{number:,.2f}"
            parts.append(escape_markdown_v2("$" + shown))
    change = entry.get("change_pct")
    if change is not None:
        try:
            moved = escape_markdown_v2(f"{float(change):+.1f}%")
        except (TypeError, ValueError):
            pass
        else:
            parts.append(f"{_rise_emoji(change)} *{moved}*")
    relative_volume = entry.get("relative_volume")
    if relative_volume is not None:
        try:
            volume = escape_markdown_v2(f"{float(relative_volume):.1f}\u00d7")
        except (TypeError, ValueError):
            pass
        else:
            parts.append(f"RVOL *{volume}*")
    score = entry.get("score")
    if score is not None:
        try:
            rated = escape_markdown_v2(f"{float(score):.0f}")
        except (TypeError, ValueError):
            pass
        else:
            parts.append(f"score *{rated}*")
    return "  \u00b7  ".join(parts)


def format_market_report_post_md(report, *, origin):
    """Pre-market or post-market briefing in one message."""

    raw_type = str(report.get("report_type") or "")
    label = escape_markdown_v2(
        str(
            report.get("label")
            or ("Pre-market briefing" if raw_type == "pre_market" else "Post-market recap")
        )
    )
    header = f"\U0001F9ED *{label}*"
    headline = escape_markdown_v2(str(report.get("headline") or "").strip())
    summary = escape_markdown_v2(str(report.get("summary") or "").strip())
    blocks = [header]
    if headline:
        blocks.append(headline)
    if summary and summary != headline:
        blocks.append(summary)
    leaders = report.get("leaders") or []
    if isinstance(leaders, list) and leaders:
        lines = []
        for leader in leaders[:MARKET_REPORT_LEADER_LIMIT]:
            if not isinstance(leader, dict):
                continue
            ticker = escape_markdown_v2(str(leader.get("ticker") or "").strip().upper())
            if not ticker:
                continue
            metrics = _format_metrics_line(leader)
            lines.append((f"*{ticker}* " + (metrics or "")).strip())
        if lines:
            blocks.append("\n".join(lines))
    base = origin.rstrip("/")
    day = str(report.get("report_day") or "").strip()
    slug = "pre" if raw_type == "pre_market" else "post"
    path = f"{base}/reports/{day}/{slug}" if day else base
    blocks.append(markdown_link("Open report", path))
    return _join_blocks(blocks)


def format_public_report_post_md(report, *, origin):
    """A research report that just went public."""

    ticker_raw = str(report.get("ticker") or "").strip()
    sports = ticker_raw.lower().startswith("sports:")
    token = ticker_raw.upper().lstrip("$") if ticker_raw else ""
    base = origin.rstrip("/")
    header = (
        "\U0001F4C4 *New public report*"
        if not token or sports
        else f"\U0001F4C4 *New public report \u00b7 ${escape_markdown_v2(token)}*"
    )
    blocks = [header]
    headline = escape_markdown_v2(str(report.get("headline") or "").strip())
    if headline:
        blocks.append(headline)
    # One URL per message, and the report page is the one that carries a card.
    # The ticker is already named in the header, so a second link to /t/ only
    # spent a line the reader could not preview.
    public_id = str(report.get("public_id") or "").strip()
    if public_id:
        blocks.append(markdown_link("Read report", f"{base}/research/{public_id}"))
    elif not sports and token:
        blocks.append(markdown_link(f"${token}", f"{base}/t/{token}"))
    return _join_blocks(blocks)


def format_event_post_md(event, *, origin):
    """A new filing or market event on a tracked ticker."""

    ticker_raw = str(event.get("ticker") or "").strip().upper()
    ticker = escape_markdown_v2(ticker_raw)
    kind = escape_markdown_v2(str(event.get("kind") or "Filing update"))
    headline = escape_markdown_v2(str(event.get("headline") or "").strip())
    age = escape_markdown_v2(str(event.get("age") or "").strip())
    base = origin.rstrip("/")
    head_title = f"\U0001F4F0 *Event on ${ticker}*" if ticker else "\U0001F4F0 *New event*"
    blocks = [head_title, f"*{kind}*"]
    if headline:
        blocks.append(headline)
    if age:
        is_sec = "sec" in str(event.get("source") or "").lower()
        sec_path = " \u00b7 filed via SEC" if is_sec else ""
        blocks.append(f"\u00b7 {age} ago{sec_path}")
    if ticker:
        blocks.append(markdown_link(f"${ticker_raw}", f"{base}/t/{ticker_raw}"))
    return _join_blocks(blocks)


def format_board_segment_md(runners, *, origin):
    """The "New on the Board" segment: the whole batch, one card.

    Telegram previews exactly one URL per message, so a link per runner spent
    nine lines to render one arbitrary thumbnail. The batch is named in text and
    the single link goes to the leader, whose ticker page carries a card. The
    other names earn their own card later, when their free Flash report
    publishes.
    """

    base = origin.rstrip("/")
    entries = [row for row in (runners or []) if row.get("ticker")]
    if not entries:
        return ""
    header = (
        "\U0001F406 *1 new on the board*"
        if len(entries) == 1
        else f"\U0001F406 *{len(entries)} new on the board*"
    )
    blocks = [header]
    for entry in entries:
        ticker = escape_markdown_v2(str(entry.get("ticker") or "").strip().upper())
        emoji = _state_emoji(entry.get("tag") or "")
        head = (emoji + " " if emoji else "") + f"*{ticker}*"
        state = escape_markdown_v2(str(entry.get("tag") or ""))
        if state:
            head += f"  \u2014  {state}"
        blocks.append(head)
        metrics = _format_metrics_line(entry)
        if metrics:
            blocks.append(metrics)
    lead = str(entries[0].get("ticker") or "").strip().upper()
    label = f"${lead}" if len(entries) == 1 else f"${lead} and the rest of the board"
    blocks.append(markdown_link(label, f"{base}/t/{lead}"))
    return _join_blocks(blocks)


def format_release_announcement_md(version, notes, *, origin):
    """Build note + link in Markdown V2."""

    text = " ".join(str(notes or "").split())
    if not text:
        return ""
    # Trim the raw note, not the escaped one: slicing after escaping can cut a
    # backslash off the character it escapes and leave a dangling escape.
    safe = escape_markdown_v2(text[:RELEASE_NOTES_LIMIT].rstrip())
    base = origin.rstrip("/")
    blocks = [
        f"\U0001F406 *RATi Runners {escape_markdown_v2(version)}*",
        safe,
        markdown_link("Open runners", base),
    ]
    return _join_blocks(blocks)
