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
    fields = {"chat_id": config.chat_id, "caption": caption, "disable_notification": "false"}
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


def _price_label(value: Any) -> str:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return "price n/a"
    if price < 1:
        return f"${price:.4f}"
    return f"${price:,.2f}"


def _change_label(value: Any) -> str:
    try:
        change = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{change:+.2f}%"


def _relative_volume_label(value: Any) -> str:
    try:
        relative_volume = float(value)
    except (TypeError, ValueError):
        return ""
    return f"RVOL {relative_volume:.1f}x"


def format_runner_line(entry: Mapping[str, Any], *, origin: str) -> str:
    """Format one runner as a metrics line plus its ticker link."""

    ticker = str(entry.get("ticker") or "").strip().upper()
    facts = [f"${ticker}", _price_label(entry.get("price"))]
    extras = (
        _change_label(entry.get("change_pct")),
        _relative_volume_label(entry.get("relative_volume")),
    )
    facts.extend(label for label in extras if label)
    facts.append(f"score {_score(entry):.0f}")
    return " · ".join(facts) + f"\n{origin.rstrip('/')}/t/{ticker}"


def format_runner_digest(entries: Iterable[Mapping[str, Any]], *, origin: str) -> str:
    """Build one Telegram message for a group of new runners."""

    rows = list(entries)
    count = len(rows)
    header = "🟢 1 new runner detected" if count == 1 else f"🟢 {count} new runners detected"
    blocks = [format_runner_line(entry, origin=origin) for entry in rows]
    message = "\n\n".join([header, *blocks])
    return message[:MAX_MESSAGE_CHARS]


def format_market_report_post(report: Mapping[str, Any], *, origin: str) -> str:
    """Build one Telegram message for a frozen pre-market or post-market report."""

    report_type = str(report.get("report_type") or "")
    label = str(
        report.get("label")
        or ("Pre-market briefing" if report_type == "pre_market" else "Post-market recap")
    )
    header = f"📋 {label}"
    headline = str(report.get("headline") or "").strip()
    summary = str(report.get("summary") or "").strip()
    blocks = [header]
    if headline:
        blocks.append(headline)
    if summary and summary != headline:
        blocks.append(summary)
    leaders: list[str] = []
    raw_leaders = report.get("leaders") or []
    if isinstance(raw_leaders, list):
        for leader in raw_leaders[:MARKET_REPORT_LEADER_LIMIT]:
            if not isinstance(leader, Mapping):
                continue
            ticker = str(leader.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            facts = [f"${ticker}"]
            change = _change_label(leader.get("change_pct"))
            if change:
                facts.append(change)
            if leader.get("score") is not None:
                facts.append(f"score {_score(leader):.0f}")
            leaders.append(" · ".join(facts))
    if leaders:
        blocks.append("\n".join(leaders))
    path = str(report.get("path") or "").strip()
    if not path:
        day = str(report.get("report_day") or "").strip()
        slug = "pre" if report_type == "pre_market" else "post"
        path = f"/reports/{day}/{slug}" if day else ""
    if path:
        blocks.append(f"{origin.rstrip('/')}{path}")
    return "\n\n".join(blocks)[:MAX_MESSAGE_CHARS]


def format_public_report_post(report: Mapping[str, Any], *, origin: str) -> str:
    """Build one Telegram message for a research report that just went public."""

    ticker = str(report.get("ticker") or "").strip()
    sports = ticker.lower().startswith("sports:")
    symbol = ticker.upper().lstrip("$")
    header = "📄 New public report" if sports or not symbol else f"📄 New public report · ${symbol}"
    blocks = [header]
    headline = str(report.get("headline") or "").strip()
    if headline:
        blocks.append(headline)
    base = origin.rstrip("/")
    public_id = str(report.get("public_id") or "").strip()
    if not sports and symbol:
        blocks.append(f"{base}/t/{symbol}")
    if public_id:
        blocks.append(f"{base}/research/{public_id}")
    return "\n".join(blocks)[:MAX_MESSAGE_CHARS]


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


def format_update_announcement(activity: Mapping[str, Any], *, origin: str) -> str:
    """One message for everything that just landed, in the room's plain voice.

    Used when no model writes the announcement, and as the fallback when one
    fails. Everything named here came from a stored row, so the links and numbers
    are the ones the site already shows.
    """

    base = origin.rstrip("/")

    def link(row: Mapping[str, Any]) -> str:
        url = str(row.get("url") or "").strip()
        if url:
            return url
        path = str(row.get("path") or "").strip()
        return f"{base}{path}" if path else ""

    runners = [row for row in activity.get("runners") or [] if row.get("ticker")]
    reports = list(activity.get("reports") or [])
    total = len(runners) + len(reports)
    if total == 0:
        return ""
    header = "🐆 1 new on the board" if total == 1 else f"🐆 {total} new on the board"
    blocks = [header]
    for entry in runners:
        symbol = str(entry.get("ticker") or "").strip().upper()
        facts = [f"${symbol}"]
        change = _change_label(entry.get("change_pct"))
        if change:
            facts.append(change)
        relative_volume = _relative_volume_label(entry.get("relative_volume"))
        if relative_volume:
            facts.append(relative_volume)
        line = " · ".join(facts)
        url = link(entry)
        if url:
            line += f"\n{url}"
        blocks.append(line)
    for report in reports:
        label = str(report.get("label") or "Report").strip()
        headline = str(report.get("headline") or "").strip()
        line = f"{label}: {headline}" if headline else label
        url = link(report)
        if url:
            line += f"\n{url}"
        blocks.append(line)
    return "\n\n".join(blocks)[:MAX_MESSAGE_CHARS]


def format_release_announcement(version: str, notes: str | None, *, origin: str) -> str:
    """One message announcing a release, from its notes.

    There is no message without notes: a bare build hash tells the room nothing,
    so a deploy is only announced when there is something to say about it.
    """

    text = " ".join(str(notes or "").split())
    if not text:
        return ""
    blocks = [f"🐆 RATi Runners {version}", text[:1000], origin.rstrip("/")]
    return "\n\n".join(blocks)[:MAX_MESSAGE_CHARS]


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
    with opener(request, timeout=SEND_TIMEOUT_SECONDS) as response:
        status = getattr(response, "status", 200)
        body = response.read()
        if status >= 400:
            details = (
                body.decode("utf-8", errors="ignore")
                if isinstance(body, bytes)
                else str(body)
            )
            raise RuntimeError(f"Telegram {method} failed with status {status}: {details[:200]}")
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


def send_message(
    config: TelegramConfig,
    text: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> None:
    """Post one message. Raises on transport or API errors."""

    if not config.configured:
        raise RuntimeError("Telegram bot token and chat id are required")
    payload = json.dumps(
        {
            "chat_id": config.chat_id,
            "text": text,
            "disable_notification": False,
        }
    ).encode()
    request = urllib.request.Request(
        config.endpoint(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=SEND_TIMEOUT_SECONDS) as response:
        status = getattr(response, "status", 200)
        if status >= 400:
            raise urllib.error.HTTPError(
                config.endpoint(), status, "Telegram sendMessage failed", {}, None
            )
        response.read()

# ---------------------------------------------------------------------------
# Markdown V2 formatters and link-preview sender.
#
# The formatters below produce Telegram Markdown V2 so the channel reads
# like a message board: bold tickers, emoji-coded state, an inline metrics
# line per runner, and exactly one URL per block so Telegram unfurls a
# link preview from the entity's ticker page. Every user-provided string
# is escaped with escape_markdown_v2, every URL is the first URL in the
# message, and send_post switches off preview for explicit cases. Telegram
# parse failures fall back to plain text so nothing is dropped silently.
# ---------------------------------------------------------------------------

_MD_V2_SPECIAL = chr(0) + "_*[]()~`>#+-=|{}.!" + chr(92)
# Characters Telegram treats as syntax anywhere in the line.
_MD_V2_INLINE_SPECIAL = "_*[]()~`" + chr(92)
# Characters Telegram only treats as syntax at the start of a line or after whitespace.
_MD_V2_LINE_SPECIAL = ">#+-=|{}.!"
_INLINE_RE = re.compile("([" + re.escape(_MD_V2_INLINE_SPECIAL) + "])")
_LINE_RE = re.compile(r"(^|(?<=\s))([" + re.escape(_MD_V2_LINE_SPECIAL) + "])")


def escape_markdown_v2(text):
    """Escape Markdown V2 special characters; line-context only for the ones that
    only matter at line start or after whitespace."""

    if not text:
        return ""
    out = _INLINE_RE.sub(r"\\\1", text)
    out = _LINE_RE.sub(lambda m: m.group(1) + "\\" + m.group(2), out)
    return out


def _first_url(text):
    match = re.search(r"https?://[^\s)]+", text)
    return match.group(0) if match else ""


def send_post(config, text, *, preview_url="", parse_mode="MarkdownV2", opener=None):
    """Send one message with a link preview from its first URL.

    Telegram renders a preview for the first URL it finds in the message body.
    To pin which link unfurls, pass preview_url and we surface it as the
    first line. When the Markdown parse fails, the message is resent plain so
    we never drop a notification silently.
    """

    body = text[:MAX_MESSAGE_CHARS]
    if preview_url and (preview_url not in body):
        body = f"{preview_url}\n\n{body}"
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
        plain = {"chat_id": config.chat_id, "text": body, "disable_notification": False}
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
    parts = []
    price = entry.get("price")
    if price is not None:
        try:
            number = float(price)
            parts.append("$" + (f"{number:.4f}" if number < 1 else f"{number:,.2f}"))
        except (TypeError, ValueError):
            pass
    change = entry.get("change_pct")
    if change is not None:
        try:
            arrow = _rise_emoji(change)
            parts.append(f"{arrow} *{change:+.1f}%*")
        except (TypeError, ValueError):
            pass
    relative_volume = entry.get("relative_volume")
    if relative_volume is not None:
        try:
            parts.append(f"RVOL *{float(relative_volume):.1f}\u00d7*")
        except (TypeError, ValueError):
            pass
    score = entry.get("score")
    if score is not None:
        try:
            parts.append(f"score *{float(score):.0f}*")
        except (TypeError, ValueError):
            pass
    return "  \u00b7  ".join(parts)


def format_runner_digest_md(entries, *, origin):
    rows = list(entries)
    count = len(rows)
    header = (
        "\U0001F7E2 *1 new runner detected*"
        if count == 1
        else f"\U0001F7E2 *{count} new runners detected*"
    )
    base = origin.rstrip("/")
    blocks = [header]
    for entry in rows:
        ticker = escape_markdown_v2(str(entry.get("ticker") or "").strip().upper())
        state = escape_markdown_v2(str(entry.get("tag") or ""))
        emoji = _state_emoji(entry.get("tag") or "")
        url = f"{base}/t/{ticker}"
        metrics = _format_metrics_line(entry)
        head = (emoji + " " if emoji else "") + f"*{ticker}*"
        if state:
            head += f"  \u2014  *{state}*"
        blocks.append(head)
        if metrics:
            blocks.append(metrics)
        else:
            blocks.append(escape_markdown_v2(str(entry.get("company") or "")))
        blocks.append(url)
    return "\n\n".join(blocks)[:MAX_MESSAGE_CHARS]


def format_market_report_post_md(report, *, origin):
    """Pre-market or post-market briefing in one message."""

    raw_type = str(report.get("report_type") or "")
    label = escape_markdown_v2(str(
        report.get("label")
        or ("Pre-market briefing" if raw_type == "pre_market" else "Post-market recap")
    ))
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
    blocks.append(path)
    return "\n\n".join(blocks)[:MAX_MESSAGE_CHARS]


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
    if not sports and token:
        blocks.append(f"{base}/t/{escape_markdown_v2(token)}")
    public_id = escape_markdown_v2(str(report.get("public_id") or "").strip())
    if public_id:
        blocks.append(f"{base}/research/{public_id}")
    return "\n\n".join(blocks)[:MAX_MESSAGE_CHARS]


def format_event_post_md(event, *, origin):
    """A new filing or market event on a tracked ticker."""

    ticker_raw = str(event.get("ticker") or "")
    ticker = escape_markdown_v2(ticker_raw.strip().upper())
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
        blocks.append(f"{base}/t/{ticker}")
    return "\n\n".join(blocks)[:MAX_MESSAGE_CHARS]


def format_update_announcement_md(activity, *, origin):
    base = origin.rstrip("/")
    runners = [row for row in (activity.get("runners") or []) if row.get("ticker")]
    reports = list(activity.get("reports") or [])
    events = list(activity.get("events") or [])
    total = len(runners) + len(reports) + len(events)
    if total == 0:
        return ""
    header = (
        "\U0001F981 *1 new on the board*"
        if total == 1
        else f"\U0001F981 *{total} new on the board*"
    )
    blocks = [header]
    for entry in runners:
        ticker = escape_markdown_v2(str(entry.get("ticker") or "").strip().upper())
        emoji = _state_emoji(entry.get("tag") or "")
        head = (emoji + " " if emoji else "") + f"*{ticker}*"
        state = escape_markdown_v2(str(entry.get("tag") or ""))
        if state:
            head += f"  \u2014  *{state}*"
        blocks.append(head)
        metrics = _format_metrics_line(entry)
        if metrics:
            blocks.append(metrics)
        blocks.append(f"{base}/t/{ticker}")
    for event in events:
        blocks.append(format_event_post_md(event, origin=origin).splitlines()[0])
    for report in reports:
        if report.get("kind") == "market_report":
            blocks.append(format_market_report_post_md(report, origin=origin))
        else:
            blocks.append(format_public_report_post_md(report, origin=origin))
    return "\n\n".join(blocks)[:MAX_MESSAGE_CHARS]


def format_release_announcement_md(version, notes, *, origin):
    """Build note + link in Markdown V2."""

    text = " ".join(str(notes or "").split())
    if not text:
        return ""
    safe = escape_markdown_v2(text)[:900]
    base = origin.rstrip("/")
    blocks = [f"\U0001F981 *RATi Runners {escape_markdown_v2(version)}*", safe, base]
    return "\n\n".join(blocks)[:MAX_MESSAGE_CHARS]
