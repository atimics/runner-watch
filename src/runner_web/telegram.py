"""Telegram delivery for newly detected runners.

The scanner records every ticker that enters the board for the first time in
``pulse_entries``. This module turns those entries into a single digest message
and posts it to a configured chat.

The module is deliberately free of database imports so the formatting and
selection rules can be tested on their own. The scan worker in ``main`` loads
the candidate rows and records delivery outcomes.
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
DEFAULT_MIN_SCORE = 60.0
DEFAULT_MAX_PER_RUN = 10
MAX_MESSAGE_CHARS = 4096

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
        os.getenv("TELEGRAM_API_TOKEN", "").strip()
        or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
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
