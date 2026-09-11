"""Telegram delivery for public channel posts.

The channel gets a message when a new runner clears the score floor, when a
pre-market or post-market report is frozen, and when a research report becomes
public. Formatting lives here so the rules can be tested without a database.
The worker in ``main`` loads the rows and records delivery outcomes.
"""

from __future__ import annotations

import json
import logging
import os
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


def _api_call(
    config: TelegramConfig,
    method: str,
    payload: dict[str, Any],
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Any:
    """Call one Bot API method.

    The endpoint carries the bot token, so nothing here puts the URL into an error
    or a log line. Callers get the method name and the status, which is enough to
    diagnose a failure without leaking the credential.
    """

    if not config.configured:
        raise RuntimeError("Telegram bot token and chat id are required")
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
            raise RuntimeError(f"Telegram {method} failed with status {status}")
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
    opener: Callable[..., Any] = urllib.request.urlopen,
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
    opener: Callable[..., Any] = urllib.request.urlopen,
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
    opener: Callable[..., Any] = urllib.request.urlopen,
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
