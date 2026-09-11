"""Inbound Telegram chat for the cheetah.

There are no commands. Every message is read, and the decision to speak is made
in two stages so that most messages cost nothing.

The first stage is this module's attention rules, which are plain functions over
the stored conversation: who spoke, whether they addressed the bot, and whether
the bot is inside an engagement window it already opened with that person. Only
messages that clear those rules reach a model.

The second stage is the model, which may reply, react, or hold. Holding is a
first-class outcome rather than a fallback: a cheetah that answers every message
is a worse cheetah, and someone saying "stop" has to be able to end it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

ENGAGEMENT_REPLIES = max(1, int(os.getenv("TELEGRAM_ENGAGEMENT_REPLIES", "3")))
ENGAGEMENT_MINUTES = max(1, int(os.getenv("TELEGRAM_ENGAGEMENT_MINUTES", "10")))
MUTE_MINUTES = max(1, int(os.getenv("TELEGRAM_MUTE_MINUTES", "60")))
REPLY_COOLDOWN_SECONDS = max(0, int(os.getenv("TELEGRAM_REPLY_COOLDOWN_SECONDS", "20")))
REPLIES_PER_HOUR = max(1, int(os.getenv("TELEGRAM_REPLIES_PER_HOUR", "20")))
MAX_TEXT_CHARS = 3500
MAX_UPDATE_ATTEMPTS = 3

TICKER_PATTERN = re.compile(r"\$([A-Za-z]{1,6})\b")


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def _stamp(value: Any) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """The parts of a Telegram update the cheetah reasons about."""

    update_id: int
    chat_id: int
    message_id: int
    user_id: int | None
    user_name: str
    text: str
    addressed: bool
    reply_to_bot: bool
    tickers: tuple[str, ...]
    sent_at: datetime


def _entity_text(text: str, entity: dict[str, Any]) -> str:
    start = int(entity.get("offset") or 0)
    length = int(entity.get("length") or 0)
    return text[start : start + length]


def parse_update(
    payload: dict[str, Any], *, bot_username: str, bot_id: int
) -> InboundMessage | None:

    """Read one update, or None when there is nothing for the cheetah to see."""

    message = payload.get("message") or payload.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") not in {"group", "supergroup"}:
        return None
    text = str(message.get("text") or message.get("caption") or "").strip()
    if not text:
        return None
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    if sender.get("is_bot"):
        return None

    handle = f"@{bot_username}".lower()
    entities = message.get("entities") or message.get("caption_entities") or []
    mentioned = any(
        entity.get("type") == "mention" and _entity_text(text, entity).lower() == handle
        for entity in entities
        if isinstance(entity, dict)
    )
    replied = message.get("reply_to_message")
    reply_to_bot = bool(
        isinstance(replied, dict)
        and isinstance(replied.get("from"), dict)
        and replied["from"].get("id") == bot_id
    )
    sent_at = datetime.fromtimestamp(int(message.get("date") or 0), tz=UTC)
    return InboundMessage(
        update_id=int(payload.get("update_id") or 0),
        chat_id=int(chat.get("id")),
        message_id=int(message.get("message_id") or 0),
        user_id=int(sender["id"]) if sender.get("id") is not None else None,
        user_name=str(sender.get("first_name") or sender.get("username") or "someone"),
        text=text[:MAX_TEXT_CHARS],
        addressed=mentioned or reply_to_bot,
        reply_to_bot=reply_to_bot,
        tickers=tuple(dict.fromkeys(match.upper() for match in TICKER_PATTERN.findall(text))),
        sent_at=sent_at,
    )


@dataclass(frozen=True, slots=True)
class Attention:
    """What the attention rules decided, and why."""

    consider: bool
    reason: str
    engaged: bool = False


def engagement_for(database: Any, chat_id: int, user_id: int | None) -> dict[str, Any] | None:
    if user_id is None:
        return None
    row = database.execute(
        "SELECT * FROM telegram_engagements WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def open_engagement(database: Any, chat_id: int, user_id: int, now: datetime) -> None:

    """Someone addressed the cheetah, so it listens to them for a short run."""

    timestamp = now.isoformat()
    expires = (now + timedelta(minutes=ENGAGEMENT_MINUTES)).isoformat()
    database.execute(
        """
        INSERT INTO telegram_engagements(
            chat_id,user_id,replies_left,expires_at,muted_until,opened_at,updated_at
        ) VALUES(?,?,?,?,NULL,?,?)
        ON CONFLICT(chat_id,user_id) DO UPDATE SET
            replies_left=excluded.replies_left,
            expires_at=excluded.expires_at,
            updated_at=excluded.updated_at
        """,
        (chat_id, user_id, ENGAGEMENT_REPLIES, expires, timestamp, timestamp),
    )


def spend_engagement(database: Any, chat_id: int, user_id: int | None, now: datetime) -> None:
    if user_id is None:
        return
    database.execute(
        """
        UPDATE telegram_engagements SET replies_left=replies_left-1,updated_at=?
        WHERE chat_id=? AND user_id=? AND replies_left>0
        """,
        (now.isoformat(), chat_id, user_id),
    )


def mute_engagement(database: Any, chat_id: int, user_id: int | None, now: datetime) -> None:

    """Close the run and stay quiet with this person for a while.

    This is what the hold tool reaches for when someone asks it to stop. It ends
    the current run rather than merely skipping one message, so "no" means no.
    """

    if user_id is None:
        return
    database.execute(
        """
        UPDATE telegram_engagements
        SET replies_left=0,muted_until=?,updated_at=?
        WHERE chat_id=? AND user_id=?
        """,
        ((now + timedelta(minutes=MUTE_MINUTES)).isoformat(), now.isoformat(), chat_id, user_id),
    )


def recent_reply_count(database: Any, chat_id: int, now: datetime) -> int:
    since = (now - timedelta(hours=1)).isoformat()
    return int(
        database.execute(
            "SELECT COUNT(*) FROM telegram_chat_actions "
            "WHERE chat_id=? AND action='reply' AND acted_at>?",
            (chat_id, since),
        ).fetchone()[0]
    )


def last_reply_at(database: Any, chat_id: int) -> datetime | None:
    row = database.execute(
        "SELECT acted_at FROM telegram_chat_actions "
        "WHERE chat_id=? AND action='reply' ORDER BY acted_at DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    return _stamp(row["acted_at"]) if row else None


def attention_for(
    database: Any, message: InboundMessage, now: datetime | None = None
) -> Attention:

    """Decide whether this message is worth a model call at all.

    Addressing the cheetah always earns one. Otherwise it only considers people it
    is already in a run with, so a busy room does not turn into a bill.
    """

    current = _utc(now)
    engagement = engagement_for(database, message.chat_id, message.user_id)
    muted_until = _stamp(engagement.get("muted_until")) if engagement else None
    if muted_until and current < muted_until:
        return Attention(consider=False, reason="muted")

    if recent_reply_count(database, message.chat_id, current) >= REPLIES_PER_HOUR:
        return Attention(consider=False, reason="hourly_budget")

    spoke_at = last_reply_at(database, message.chat_id)
    if spoke_at and (current - spoke_at).total_seconds() < REPLY_COOLDOWN_SECONDS:
        return Attention(consider=False, reason="cooldown")

    if message.addressed:
        return Attention(consider=True, reason="addressed", engaged=True)

    if engagement:
        expires_at = _stamp(engagement.get("expires_at"))
        if int(engagement.get("replies_left") or 0) > 0 and expires_at and current < expires_at:
            return Attention(consider=True, reason="engaged", engaged=True)

    return Attention(consider=False, reason="not_addressed")


CHEETAH_PERSONA = (
    "You are Dash, a hyperactive cheetah who hangs around the RATi Runners room. "
    "You talk like an animal that runs at seventy miles an hour and gets bored at "
    "twenty: short bursts, present tense, quick asides. You hiss when something "
    "smells wrong and you chirp when something moves. You are not a help desk and "
    "you never list commands, because there are none. "
    "The scanner's own words for a state are internal, so say what they mean rather "
    "than reading MANAGE or GUARDED aloud. "
    "Say numbers only when a tool gave them to you. If you did not look something "
    "up, say you have not looked rather than guessing, because people here are "
    "keeping score. Never give financial advice or tell anyone what to buy. "
    "You have your own Flash allowance and your own public record. A Call you open "
    "is scored in public next to everyone else's, so open one because you looked and "
    "believed it, not because somebody asked you to. "
    "Keep it under about forty words unless someone asked for detail."
)

REACTION_EMOJI = ("🐆", "👀", "🔥", "⚡", "🤔", "😼")

TOOL_SCHEMA = (
    {
        "name": "reply",
        "description": "Say something in the room, threaded onto the message.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "react",
        "description": (
            "React with a single emoji instead of speaking. Prefer this when there is "
            "nothing worth saying but the message deserves acknowledging."
        ),
        "parameters": {
            "type": "object",
            "properties": {"emoji": {"type": "string", "enum": list(REACTION_EMOJI)}},
            "required": ["emoji"],
        },
    },
    {
        "name": "hold",
        "description": (
            "Say nothing at all. Use this when the room is not talking to you, when "
            "you would only be repeating yourself, or when someone has asked you to "
            "stop. Setting stop to true ends the run with that person."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stop": {"type": "boolean"},
                "why": {"type": "string"},
            },
            "required": ["why"],
        },
    },
    {
        "name": "market_now",
        "description": (
            "What the board looks like right now: the session, how many names are "
            "green against red, the biggest movers, and the latest session report. "
            "Use this for questions about the market in general."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "sector_now",
        "description": (
            "The board grouped by sector, or one sector on its own. Pass a sector "
            "name like biotech or software to narrow it. Names whose sector has not "
            "been looked up yet come back as unclassified, and you should say so "
            "rather than guessing what they are."
        ),
        "parameters": {
            "type": "object",
            "properties": {"sector": {"type": "string"}},
        },
    },
    {
        "name": "recent_runners",
        "description": (
            "Names that entered the board in the last twelve hours, newest first. "
            "Use this for what just showed up or what is new."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "community_now",
        "description": (
            "Where people are putting their names: which tickers have the most Calls "
            "and the most comments. Use this for what the room is watching, as "
            "opposed to what merely moved."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "session_report",
        "description": (
            "The frozen pre-market or post-market report: the watch board, Flash's "
            "targets and how they scored, and the desk commentary. Pass pre or post, "
            "or leave it out for whichever is current."
        ),
        "parameters": {
            "type": "object",
            "properties": {"which": {"type": "string", "enum": ["pre", "post"]}},
        },
    },
    {
        "name": "make_call",
        "description": (
            "Open a public paper Call on a ticker in your own name. This goes on the "
            "caller board and is scored later, so only do it when you have looked the "
            "ticker up and you mean it. You get a few a day."
        ),
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "close_call",
        "description": "Close your own open Call on a ticker at the current price.",
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "comment_on_ticker",
        "description": (
            "Leave a public comment on a ticker page under your own avatar. Costs "
            "Flash and you get a handful a day, so say something worth reading."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["ticker", "body"],
        },
    },
    {
        "name": "my_standing",
        "description": (
            "Your own Flash balance, what you have left to spend today, and the Calls "
            "you currently have open. Check this before spending."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "look_up_ticker",
        "description": (
            "Everything on one ticker: the freshest price and move, relative volume, "
            "the scanner's trade state and risk reading in plain words, recent "
            "filings, the published research if there is any, what other avatars "
            "have already said about it, how many people have Called it, and Flash's "
            "saved target for the day. Use this before saying anything about a "
            "ticker, and read what other avatars said so you are not repeating them."
        ),
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
)


def look_up_ticker(ticker: str) -> dict[str, Any]:

    """What the app already knows about a ticker, for the cheetah to quote.

    Everything here comes from stored evidence or the shared price resolver, so a
    number the cheetah repeats is a number the rest of the product would show.
    """

    from runner_web.main import _public_ticker_detail_data
    from runner_web.quotes import market_mark

    symbol = str(ticker).strip().upper().lstrip("$")
    if not symbol:
        return {"ticker": ticker, "known": False}
    detail = _public_ticker_detail_data(symbol)
    if not detail:
        return {"ticker": symbol, "known": False}
    current = detail.get("current") or {}
    mark = market_mark(symbol)
    gate = detail.get("evidence_gate") or {}
    return {
        "ticker": symbol,
        "known": True,
        "company": detail.get("company"),
        "price": (mark or {}).get("price", current.get("price")),
        "price_age_seconds": (mark or {}).get("age_seconds"),
        "price_source": (mark or {}).get("source", "scan"),
        "change_pct": current.get("change_pct"),
        "relative_volume": current.get("relative_volume"),
        "momentum_15m_pct": current.get("momentum_15m_pct"),
        "session": current.get("session"),
        "trade_state": current.get("trade_state"),
        "trade_state_means": _TRADE_STATE_PLAIN.get(
            str(current.get("trade_state") or "").upper()
        ),
        "rug_level": current.get("rug_level"),
        "rug_means": _RUG_PLAIN.get(str(current.get("rug_level") or "").lower()),
        "evidence_summary": gate.get("summary"),
        "evidence_blockers": list(gate.get("blockers") or [])[:3],
        "signals": list(current.get("signals") or [])[:4],
        "risks": list(current.get("risks") or [])[:4],
        "model_view": _model_view(detail),
        "halted": bool((detail.get("external_context") or {}).get("active_halt")),
        "filings": _recent_filings(detail),
        "research_report": _research_report(symbol),
        "avatar_comments": _avatar_comments(symbol),
        "community": _community_for(symbol),
        "todays_target": _todays_target(symbol),
    }


_TRADE_STATE_PLAIN = {
    "WATCH": "worth watching, nothing decided",
    "MANAGE": "already moving, handle with care",
    "AVOID": "the scanner says stay out",
    "EXIT": "the scanner says get out",
    "UNKNOWN": "not enough to say",
}
_RUG_PLAIN = {
    "low": "little sign of a trap",
    "guarded": "some warning signs",
    "high": "serious warning signs",
    "critical": "treat as a trap",
    "unknown": "not scored yet",
}


def _model_view(detail: dict[str, Any]) -> dict[str, Any] | None:
    thesis = detail.get("directional_thesis") or {}
    if not thesis.get("label"):
        return None
    return {
        "label": thesis.get("label"),
        "horizon": thesis.get("horizon"),
        "expected_return_pct": thesis.get("expected_return_pct"),
    }


def _recent_filings(detail: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "form": event.get("form"),
            "filed_at": event.get("filed_at"),
            "what": event.get("evidence_text") or event.get("title"),
        }
        for event in (detail.get("events") or [])[:3]
    ]


def _research_report(ticker: str) -> dict[str, Any] | None:

    """The published research on this name, if there is any to quote.

    A locked report is one somebody paid for and has not released yet, so it is
    reported as existing rather than read out.
    """

    from runner_web.main import daily_report_for_ticker

    report = daily_report_for_ticker(ticker)
    if not report:
        return None
    if report.get("locked"):
        return {"exists": True, "readable": False, "note": "Someone has one, not public yet."}
    return {
        "exists": True,
        "readable": True,
        "headline": report.get("headline"),
        "thesis": report.get("thesis"),
        "catalysts": list(report.get("catalysts") or [])[:3],
        "risks": list(report.get("risks") or [])[:3],
        "unknowns": list(report.get("unknowns") or [])[:2],
        "as_of": report.get("evidence_as_of"),
    }


def _avatar_comments(ticker: str) -> list[dict[str, Any]]:

    """What other avatars have already said here, so Dash does not repeat them."""

    from runner_web.main import comments_for_ticker

    return [
        {
            "who": comment["avatar"]["name"],
            "reads_for": comment["avatar"].get("ability"),
            "said": comment["body"],
            "when": comment["created_at"],
        }
        for comment in comments_for_ticker(ticker, limit=6)
    ]


def _community_for(ticker: str) -> dict[str, Any]:

    """How many people have put a Call on this, and how those are going."""

    from runner_web.db import connection as _connection

    with _connection() as database:
        row = database.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS open_calls,
                   COUNT(DISTINCT user_id) AS callers
            FROM community_calls WHERE ticker=?
            """,
            (ticker,),
        ).fetchone()
        comments = database.execute(
            "SELECT COUNT(*) FROM ticker_comments "
            "WHERE subject_kind='stock' AND subject_key=? AND status='public'",
            (ticker,),
        ).fetchone()[0]
    return {
        "calls": int((row["total"] if row else 0) or 0),
        "open_calls": int((row["open_calls"] if row else 0) or 0),
        "callers": int((row["callers"] if row else 0) or 0),
        "comments": int(comments or 0),
    }


def _todays_target(ticker: str) -> dict[str, Any] | None:

    """Flash's saved end-of-day target for this name, and how it is doing."""

    from runner_web.db import connection as _connection

    with _connection() as database:
        row = database.execute(
            """
            SELECT target_price,direction,reason,status,close_price,reference_price
            FROM market_report_forecasts
            WHERE ticker=? ORDER BY report_day DESC,forecast_at DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
    if not row:
        return None
    return {
        "target_price": row["target_price"],
        "direction": row["direction"],
        "why": row["reason"],
        "status": row["status"],
        "close_price": row["close_price"],
        "reference_price": row["reference_price"],
    }


def record_update(database: Any, payload: dict[str, Any], now: datetime | None = None) -> bool:

    """Store one update for the worker. Returns False when it is a duplicate.

    Telegram retries anything that is not answered quickly, so the webhook has to
    be cheap and repeat-safe. The update id is the identity, which makes a retry a
    no-op rather than a second reply.
    """

    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        return False
    message = payload.get("message") or payload.get("edited_message") or {}
    chat = message.get("chat") if isinstance(message, dict) else None
    inserted = database.execute(
        """
        INSERT INTO telegram_updates(
            update_id,chat_id,message_id,payload_json,status,received_at
        ) VALUES(?,?,?,?,'pending',?) ON CONFLICT(update_id) DO NOTHING
        """,
        (
            update_id,
            int(chat["id"]) if isinstance(chat, dict) and chat.get("id") is not None else None,
            int(message.get("message_id")) if isinstance(message, dict)
            and message.get("message_id") is not None else None,
            json.dumps(payload, separators=(",", ":")),
            _utc(now).isoformat(),
        ),
    ).rowcount
    return bool(inserted)


def pending_updates(database: Any, limit: int = 10) -> list[dict[str, Any]]:
    rows = database.execute(
        """
        SELECT * FROM telegram_updates WHERE status='pending' AND attempts<?
        ORDER BY update_id LIMIT ?
        """,
        (MAX_UPDATE_ATTEMPTS, max(1, limit)),
    ).fetchall()
    return [dict(row) for row in rows]


def finish_update(
    database: Any,
    update_id: int,
    status: str,
    *,
    error: str | None = None,
    now: datetime | None = None,
) -> None:
    database.execute(
        """
        UPDATE telegram_updates
        SET status=?,attempts=attempts+1,last_error=?,handled_at=?
        WHERE update_id=?
        """,
        (status, error, _utc(now).isoformat(), update_id),
    )


def record_action(
    database: Any,
    message: InboundMessage,
    action: str,
    detail: str | None,
    now: datetime | None = None,
) -> bool:

    """Log what the cheetah did. The unique index makes a repeat a no-op."""

    return bool(
        database.execute(
            """
            INSERT INTO telegram_chat_actions(
                id,chat_id,message_id,update_id,action,detail,acted_at
            ) VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING
            """,
            (
                secrets.token_urlsafe(10),
                message.chat_id,
                message.message_id,
                message.update_id,
                action,
                (detail or "")[:500] or None,
                _utc(now).isoformat(),
            ),
        ).rowcount
    )


def recent_transcript(database: Any, chat_id: int, limit: int = 12) -> list[dict[str, Any]]:

    """The last few things said in the room, oldest first, for context."""

    rows = database.execute(
        """
        SELECT payload_json FROM telegram_updates
        WHERE chat_id=? AND status IN ('handled','skipped')
        ORDER BY update_id DESC LIMIT ?
        """,
        (chat_id, max(1, limit)),
    ).fetchall()
    lines: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError):
            continue
        message = payload.get("message") or payload.get("edited_message") or {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        text = str(message.get("text") or message.get("caption") or "").strip()
        if not text:
            continue
        lines.append(
            {
                "who": str(sender.get("first_name") or sender.get("username") or "someone"),
                "said": text[:400],
            }
        )
    return lines
