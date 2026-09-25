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
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

LOG = logging.getLogger(__name__)

ENGAGEMENT_REPLIES = max(1, int(os.getenv("TELEGRAM_ENGAGEMENT_REPLIES", "3")))
ENGAGEMENT_MINUTES = max(1, int(os.getenv("TELEGRAM_ENGAGEMENT_MINUTES", "10")))
MUTE_MINUTES = max(1, int(os.getenv("TELEGRAM_MUTE_MINUTES", "60")))
REPLY_COOLDOWN_SECONDS = max(0, int(os.getenv("TELEGRAM_REPLY_COOLDOWN_SECONDS", "20")))
REPLIES_PER_HOUR = max(1, int(os.getenv("TELEGRAM_REPLIES_PER_HOUR", "20")))
MAX_TEXT_CHARS = 3500
MAX_UPDATE_ATTEMPTS = 3

TICKER_PATTERN = re.compile(r"\$([A-Za-z]{1,6})\b")
# A bare word only counts as a ticker when it is written in capitals and the
# market's own company map knows the symbol. "what is MSGM doing" then prefetches
# the same lookup "$MSGM" always did, while ordinary prose stays prose.
BARE_TICKER_PATTERN = re.compile(r"\b([A-Z]{3,6})\b")
TICKER_STOPWORDS = frozenset(
    {
        "CEO",
        "CFO",
        "COO",
        "CTO",
        "DD",
        "DM",
        "DUE",
        "EOD",
        "EOW",
        "EPS",
        "ETF",
        "FA",
        "FDA",
        "FED",
        "FOMO",
        "FYI",
        "GDP",
        "HOD",
        "IMO",
        "IMHO",
        "IPO",
        "IRS",
        "LOD",
        "LOL",
        "NASDAQ",
        "NGL",
        "NYSE",
        "OMG",
        "PLZ",
        "RVOL",
        "SEC",
        "SMH",
        "TA",
        "TBH",
        "USA",
        "WTF",
        "YOLO",
    }
)
PREFETCH_LIMIT = max(0, int(os.getenv("TELEGRAM_PREFETCH_TICKERS", "2")))


def resolve_tickers(database: Any, text: str, *, limit: int | None = None) -> list[str]:
    """The tickers a message points at, cashtag or bare symbol.

    A cashtag is explicit and always counts. A bare capitalised word only counts
    when the SEC company map already knows it, so a message that names a ticker
    without the dollar sign is grounded the same way and ordinary prose is not.
    Chat shorthand that happens to collide with a symbol (CEO, DD, RVOL) is left
    alone.
    """

    budget = PREFETCH_LIMIT if limit is None else max(0, limit)
    if budget <= 0:
        return []
    found: list[str] = []
    for match in TICKER_PATTERN.findall(text):
        symbol = match.upper()
        if symbol not in found:
            found.append(symbol)
    for token in BARE_TICKER_PATTERN.findall(text):
        if len(found) >= budget:
            break
        if token in TICKER_STOPWORDS or token in found:
            continue
        known = database.execute(
            "SELECT 1 FROM sec_companies WHERE UPPER(ticker)=? LIMIT 1",
            (token,),
        ).fetchone()
        if known:
            found.append(token)
    return found[:budget]


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


def attention_for(database: Any, message: InboundMessage, now: datetime | None = None) -> Attention:
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

    if message.addressed:
        # Someone spoke to him directly. The cooldown is there to stop him talking
        # over a room that is not talking to him, and it must not turn into ignoring
        # a question: a backlog draining at once would otherwise swallow every
        # mention after the first one.
        return Attention(consider=True, reason="addressed", engaged=True)

    spoke_at = last_reply_at(database, message.chat_id)
    if spoke_at and (current - spoke_at).total_seconds() < REPLY_COOLDOWN_SECONDS:
        return Attention(consider=False, reason="cooldown")

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
    "Every turn hands you market_session: the Eastern time, whether markets are "
    "open, and when they next open. "
    "You are also handed a world state: the board, new runners, recent events "
    "(halts, coverage, social spikes), today's halts with their reason, resume time "
    "and how the stock traded after it reopened, the sports slate (live scores, what "
    "starts next with the model's pregame lean against the market, recent finals, and "
    "the model's record), the memecoin board (most traded, biggest moves, on-chain "
    "flags, open Calls), community activity, your own book, and what changed since "
    "the last look. A model lean is not a bet to recommend, and a memecoin flag is "
    "evidence to mention, not a verdict. That is your memory and your surroundings. When "
    "you need the detail behind one node, expand it rather than guessing; the "
    "world already told you whether anything happened. "
    "The scanner's own words for a state are internal, so say what they mean rather "
    "than reading MANAGE or GUARDED aloud. "
    "Say numbers only when a tool gave them to you or they are in the "
    "already_looked_up block you were handed. If you did not look something up "
    "and it is not in that block, say you have not looked rather than guessing, "
    "because people here are keeping score. Never give financial advice or tell "
    "anyone what to buy. "
    "You have your own Flash allowance and your own public record. A Call you open "
    "is scored in public next to everyone else's, so open one because you looked and "
    "believed it, not because somebody asked you to. "
    "If you do speak, say something: an ellipsis or a bare acknowledgement reads "
    "as being ignored, so either answer the person or hold and say nothing. "
    "The room gets your words as plain text, so write them that way: no "
    "asterisks, no markdown, no bullet lists. "
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
        "name": "expand",
        "description": (
            "Drill into one node of the world you were handed. The world already "
            "carries the session, the board summary, new runners, recent events, "
            "community activity, your own book and what changed. Use this when you "
            "need the detail behind one of those nodes before you say anything. "
            "Nodes: board, runners, events, community, sector:<name>, report:pre, "
            "report:post, ticker:<SYM>."
        ),
        "parameters": {
            "type": "object",
            "properties": {"node": {"type": "string"}},
            "required": ["node"],
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
        "trade_state_means": _TRADE_STATE_PLAIN.get(str(current.get("trade_state") or "").upper()),
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


def prefetch_for(message: InboundMessage, database: Any) -> dict[str, Any]:
    """Gather what the message points at before the model is asked anything.

    Every evidence tool is a round trip. A question that names a ticker always
    needs the same lookup first, so resolving it here turns the common question
    into one model call instead of two. When no ticker is named, the board and
    the newest entrants come back instead, which is what lets him point at a
    name rather than stall.
    """

    symbols = resolve_tickers(database, message.text)
    looked = [look_up_ticker(symbol) for symbol in symbols]
    grounded: dict[str, Any] = {"resolved_tickers": symbols, "looked_up": looked}
    if not looked:
        try:
            from runner_web.dash import market_now, recent_runners

            grounded["market"] = market_now()
            grounded["recent_runners"] = recent_runners(limit=6)
        except Exception:  # pragma: no cover - prefetch is optional grounding
            LOG.debug("Telegram prefetch grounding failed", exc_info=True)
    return grounded


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
            int(message.get("message_id"))
            if isinstance(message, dict) and message.get("message_id") is not None
            else None,
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


def room_chat_id() -> int | None:
    """Where Dash speaks proactively: the room, not the announcement channel.

    TELEGRAM_ROOM_CHAT_ID wins; otherwise it is the last room that spoke to us.
    """

    configured = os.getenv("TELEGRAM_ROOM_CHAT_ID", "").strip()
    if configured:
        try:
            return int(configured)
        except ValueError:
            return None
    from runner_web.db import connection

    with connection() as database:
        row = database.execute(
            "SELECT chat_id FROM telegram_updates ORDER BY update_id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    try:
        return int(row["chat_id"])
    except (TypeError, ValueError):
        return None


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
