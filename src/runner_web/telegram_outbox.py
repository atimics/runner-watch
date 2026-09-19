"""Frozen announcement cards, shared channel pacing, and confirmed delivery history."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from runner_web.db import connection
from runner_web.telegram import (
    MAX_MESSAGE_CHARS,
    TelegramDeliveryError,
    escape_markdown_v2,
    format_event_post_md,
    markdown_link,
    message_units,
    next_segment,
    strip_markdown_v2,
)


def _setting(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def label(value, limit: int = 180) -> str:
    text = re.sub(r"https?://\S+", "", str(value or ""))
    text = " ".join(text.split())
    if message_units(text) <= limit:
        return text
    return text.encode("utf-16-le")[: (limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def lock_channel(database, chat_id: str, current: datetime):
    """Take the channel row lock before any message row locks."""
    day = current.date().isoformat()
    database.execute(
        "INSERT INTO telegram_channel_schedule(chat_id,next_at,budget_day,used) "
        "VALUES(?,'',?,0) ON CONFLICT(chat_id) DO NOTHING",
        (chat_id, day),
    )
    return database.execute(
        "UPDATE telegram_channel_schedule SET next_at=next_at WHERE chat_id=? "
        "RETURNING next_at,budget_day,used",
        (chat_id,),
    ).fetchone()


def reserve_channel_slot(database, chat_id: str, tickers: list[str], current: datetime) -> bool:
    """Reserve one channel attempt inside the caller's database transaction."""
    stamp, day = current.isoformat(), current.date().isoformat()
    row = lock_channel(database, chat_id, current)
    used = int(row["used"]) if row["budget_day"] == day else 0
    if row["next_at"] > stamp or used >= _setting("TELEGRAM_CHANNEL_DAILY_LIMIT", 24):
        return False
    for ticker in set(tickers):
        quiet = database.execute(
            "SELECT next_at FROM telegram_ticker_schedule WHERE chat_id=? AND ticker=?",
            (chat_id, ticker),
        ).fetchone()
        if quiet and quiet["next_at"] > stamp:
            return False
    next_at = (
        current
        + timedelta(
            seconds=_setting(
                "TELEGRAM_CHANNEL_INTERVAL_SECONDS",
                _setting("TELEGRAM_SEGMENT_GAP_MINUTES", 12) * 60,
            )
        )
    ).isoformat()
    database.execute(
        "UPDATE telegram_channel_schedule SET next_at=?,budget_day=?,used=? WHERE chat_id=?",
        (next_at, day, used + 1, chat_id),
    )
    quiet_until = (
        current + timedelta(seconds=_setting("TELEGRAM_TICKER_QUIET_SECONDS", 1800))
    ).isoformat()
    for ticker in set(tickers):
        database.execute(
            "INSERT INTO telegram_ticker_schedule(chat_id,ticker,next_at) VALUES(?,?,?) "
            "ON CONFLICT(chat_id,ticker) DO UPDATE SET next_at=excluded.next_at",
            (chat_id, ticker, quiet_until),
        )
    return True


def split_cards(cards: list[dict]) -> list[list[dict]]:
    """Keep each complete story in its own message."""
    for card in cards:
        if not 0 < message_units(card["text"]) <= MAX_MESSAGE_CHARS:
            raise ValueError("announcement_card_size")
    return [[card] for card in cards]


def enqueue_cards(database, chat_id: str, cards: list[dict], *, at: datetime) -> int:
    # Validate every card before reserving any item identity.
    split_cards(cards)
    fresh = []
    for card in cards:
        claimed = database.execute(
            "INSERT INTO telegram_outbox_items(kind,subject,ticker,card_json) VALUES(?,?,?,?) "
            "ON CONFLICT(kind,subject) DO NOTHING RETURNING subject",
            (card["kind"], card["subject"], card.get("ticker", ""), json.dumps(card)),
        ).fetchone()
        if claimed:
            fresh.append(card)
    for position, batch in enumerate(split_cards(fresh)):
        identity = str(uuid4())
        database.execute(
            "INSERT INTO telegram_outbox(id,chat_id,text,status,created_at,updated_at,position) "
            "VALUES(?,?,?,'pending',?,?,?)",
            (
                identity,
                chat_id,
                "\n\n".join(c["text"] for c in batch),
                at.isoformat(),
                at.isoformat(),
                position,
            ),
        )
        for card in batch:
            database.execute(
                "UPDATE telegram_outbox_items SET outbox_id=? WHERE kind=? AND subject=?",
                (identity, card["kind"], card["subject"]),
            )
    return len(fresh)


def _mirror(database, cards: list[dict], status: str, attempts: int, stamp: str) -> None:
    for card in cards:
        if card["kind"] == "runner":
            database.execute(
                "INSERT INTO "
                "telegram_alert_deliveries(ticker,entered_at,status,attempts,"
                "created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(ticker,entered_at) DO UPDATE SET "
                "status=excluded.status,attempts=excluded.attempts,updated_at=excluded.updated_at",
                (card["ticker"], card["entered_at"], status, attempts, stamp, stamp),
            )
        elif card["kind"] in {"market_report", "research_report", "release"}:
            database.execute(
                "INSERT INTO "
                "telegram_channel_posts(kind,subject,status,attempts,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(kind,subject) DO UPDATE SET "
                "status=excluded.status,attempts=excluded.attempts,updated_at=excluded.updated_at",
                (card["kind"], card["subject"], status, attempts, stamp, stamp),
            )


def deliver_outbox(
    config, sender, *, at: datetime, kinds: tuple[str, ...], last_kind: str = ""
) -> dict:
    stamp = at.isoformat()
    with connection() as database:
        lock_channel(database, config.chat_id, at)
        expired = database.execute(
            "UPDATE telegram_outbox SET "
            "status='uncertain',last_error='acknowledgement_lost',updated_at=? "
            "WHERE chat_id=? AND status='sending' AND updated_at<? RETURNING id,attempts",
            (stamp, config.chat_id, (at - timedelta(minutes=5)).isoformat()),
        ).fetchall()
        for row in expired:
            cards = [
                json.loads(r[0])
                for r in database.execute(
                    "SELECT card_json FROM telegram_outbox_items WHERE outbox_id=?",
                    (row["id"],),
                ).fetchall()
            ]
            _mirror(database, cards, "uncertain", row["attempts"], stamp)
        latest = database.execute(
            "SELECT i.kind FROM telegram_outbox o JOIN telegram_outbox_items i "
            "ON i.outbox_id=o.id WHERE o.chat_id=? AND o.status='sent' "
            "ORDER BY o.updated_at DESC,o.id DESC LIMIT 1",
            (config.chat_id,),
        ).fetchone()
        if latest:
            last_kind = latest["kind"]
        placeholders = ",".join("?" for _ in kinds)
        rows = database.execute(
            "WITH eligible AS (SELECT o.*,i.card_json,ROW_NUMBER() OVER "
            "(PARTITION BY i.kind ORDER BY o.created_at,o.position,o.id) AS queue_rank "
            "FROM telegram_outbox o JOIN telegram_outbox_items i ON i.outbox_id=o.id "
            "WHERE o.chat_id=? AND o.status IN ('pending','retry') AND o.attempts<3 "
            "AND (o.retry_at IS NULL OR o.retry_at<=?) "
            f"AND i.kind IN ({placeholders})) SELECT * FROM eligible WHERE queue_rank<=50 "
            "ORDER BY created_at,position,id",
            (config.chat_id, stamp, *kinds),
        ).fetchall()
        candidates = []
        for row in rows:
            cards = [json.loads(row["card_json"])]
            if cards and cards[0].get("expires_at", "~") < stamp:
                database.execute(
                    "UPDATE telegram_outbox SET status='stale',updated_at=? WHERE id=?",
                    (stamp, row["id"]),
                )
                _mirror(database, cards, "stale", row["attempts"], stamp)
            elif cards:
                candidates.append((row, cards))
        preferred = next_segment({c[0]["kind"]: True for _, c in candidates}, last_kind=last_kind)
        candidates.sort(key=lambda item: item[1][0]["kind"] != preferred)
        chosen = None
        for row, cards in candidates:
            # The conditional update is the cross-process send claim. A denied
            # channel slot restores it within this same uncommitted transaction.
            claim = database.execute(
                "UPDATE telegram_outbox SET status='sending',attempts=attempts+1,updated_at=? "
                "WHERE id=? AND status IN ('pending','retry') AND attempts<3 "
                "RETURNING attempts",
                (stamp, row["id"]),
            ).fetchone()
            if not claim:
                continue
            tickers = [c["ticker"] for c in cards if c.get("ticker")]
            if not reserve_channel_slot(database, config.chat_id, tickers, at):
                database.execute(
                    "UPDATE telegram_outbox SET status=?,attempts=attempts-1,updated_at=? "
                    "WHERE id=?",
                    (row["status"], row["updated_at"], row["id"]),
                )
                continue
            chosen = (dict(row), cards, claim["attempts"])
            break
    if not chosen:
        return {"status": "waiting" if candidates else "empty", "sent": 0, "items": []}
    row, cards, attempts = chosen
    status, error, receipt, retry_at = "sent", None, None, None
    try:
        receipt = sender(config, row["text"])
        if type(receipt) is not int or receipt <= 0:
            raise TelegramDeliveryError("uncertain")
    except TelegramDeliveryError as exc:
        status = exc.status
        if status == "retry":
            status = "retry" if attempts < 3 else "failed"
            retry_at = (at + timedelta(seconds=exc.retry_after)).isoformat()
        error = "telegram_" + status
    except Exception:
        status, error = "uncertain", "acknowledgement_lost"
    if status != "sent":
        receipt = None
    with connection() as database:
        lock_channel(database, config.chat_id, at)
        database.execute(
            "UPDATE telegram_outbox SET status=?,message_id=?,retry_at=?,last_error=?,updated_at=? "
            "WHERE id=? AND status='sending'",
            (status, receipt, retry_at, error, stamp, row["id"]),
        )
        _mirror(database, cards, status, attempts, stamp)
        if retry_at:
            database.execute(
                "UPDATE telegram_channel_schedule SET next_at=CASE WHEN next_at<? "
                "THEN ? ELSE next_at END WHERE chat_id=?",
                (retry_at, retry_at, config.chat_id),
            )
    return {"status": status, "sent": int(status == "sent"), "items": cards, "id": row["id"]}


def stock_event_card(ticker: str, event: dict) -> dict:
    people = event["people"]
    names = "; ".join(label(p["name"], 100) + " · " + label(p["role"], 100) for p in people)
    names = label(names, 800)
    amendment = " · Amendment" if event["amendment"] else ""
    lines = [f"${ticker} · {event['action']}{amendment}", names]
    if event.get("joint"):
        lines.append("Joint filing · Shared transaction")
    for key, title in (
        ("shares", "Shares / units"),
        ("price", "Price per unit"),
        ("security", "Share class"),
        ("percent", "Reported stake (%)"),
    ):
        if event.get(key) is not None:
            lines.append(f"{title}: {label(event[key], 100)}")
    if event.get("occurred_at"):
        lines.append(f"Transaction / as-of date: {event['occurred_at']}")
    lines.append(f"Filed: {event['filed_at']}")
    return {
        "kind": "stock_filing",
        "subject": event["id"],
        "ticker": ticker,
        "group": "stock:" + ticker,
        "text": "\n".join(escape_markdown_v2(line) for line in lines),
        "event": event,
    }


def queue_stock_filings(database, config, *, origin: str, at: datetime) -> int:
    from urllib.parse import quote

    from runner_web.stock_map import FORMS, filing_events

    placeholders = ",".join("?" for _ in FORMS)
    rows = database.execute(
        f"SELECT f.* FROM sec_filings f WHERE f.form IN ({placeholders}) "
        "AND f.evidence_json<>'{}' AND NOT EXISTS "
        "(SELECT 1 FROM telegram_sec_seen s WHERE s.accession=f.accession) "
        "ORDER BY f.created_at,f.accession LIMIT 50",
        FORMS,
    ).fetchall()
    total = 0
    for row in rows:
        events = filing_events(dict(row))
        cards = []
        for event in events:
            if (
                not event.get("people")
                or not event.get("source_url")
                or event["basis"] == "Filing summary"
            ):
                continue
            card = stock_event_card(row["ticker"], event)
            card["text"] += "\n\n" + markdown_link(
                "Open map and SEC filing",
                f"{origin.rstrip('/')}/t/{quote(row['ticker'], safe='')}#ticker-map",
            )
            cards.append(card)
        total += enqueue_cards(database, config.chat_id, cards, at=at)
        database.execute(
            "INSERT INTO telegram_sec_seen(accession) VALUES(?) ON CONFLICT DO NOTHING",
            (row["accession"],),
        )
    return total


# Halt Desk / Filing Desk inventory. Events older than the window are no longer
# news, so the query stops selecting them.
TELEGRAM_EVENT_TYPES = ("trading_halt", "news_article", "social_spike")


def _event_age_label(value, current: datetime) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    minutes = max(0.0, (current - parsed).total_seconds() / 60)
    if minutes < 90:
        return f"{max(1, int(minutes))}m"
    if minutes < 60 * 36:
        return f"{int(minutes / 60)}h"
    return f"{int(minutes / (60 * 24))}d"


def _channel_event(row: dict, current: datetime) -> dict:
    """Normalize one public market event for the event formatter."""

    try:
        payload = row.get("payload_json")
        payload = payload if isinstance(payload, dict) else json.loads(payload or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    ticker = str(row.get("ticker") or "").upper()
    event_type = str(row.get("event_type") or "market_event")
    status = str(row.get("status") or "")
    if event_type == "trading_halt":
        kind = f"Trading halt · {status}" if status else "Trading halt"
        issue = str(payload.get("issue_name") or "").strip()
        market = str(payload.get("market") or "").strip()
        headline = issue or f"${ticker} halted"
        if issue and market:
            headline = f"{issue} · {market}"
    elif event_type == "news_article":
        kind = "News"
        headline = str(payload.get("title") or "New company coverage").strip()
    elif event_type == "social_spike":
        try:
            mentions = max(0, int(payload.get("mention_count") or 0))
        except (TypeError, ValueError):
            mentions = 0
        kind = f"{payload.get('network_label') or 'Social'} spike"
        headline = f"{mentions} mention{'s' if mentions != 1 else ''}"
    else:
        kind = event_type.replace("_", " ").title()
        headline = str(payload.get("title") or "").strip()
    return {
        "subject": "|".join(
            str(row.get(key) or "") for key in ("source", "feed", "event_id", "version")
        ),
        "ticker": ticker,
        "kind": kind,
        "headline": headline,
        "source": str(row.get("source") or ""),
        "age": _event_age_label(row.get("event_at"), current),
        "at": row.get("event_at"),
        "url": str(row.get("source_url") or ""),
    }


def event_card(event: dict, *, origin: str) -> dict:
    return {
        "kind": "event",
        "subject": event["subject"],
        "ticker": event.get("ticker", ""),
        "group": "ticker:" + str(event.get("ticker") or ""),
        "text": format_event_post_md(event, origin=origin),
        "event": event,
    }


def queue_events(database, config, *, origin: str, at: datetime) -> int:
    """Queue halts, coverage and social spikes from the recent window."""

    cutoff = (at - timedelta(minutes=_setting("TELEGRAM_EVENT_WINDOW_MINUTES", 360))).isoformat()
    placeholders = ",".join("?" for _ in TELEGRAM_EVENT_TYPES)
    rows = database.execute(
        f"""
        SELECT source,feed,event_id,version,ticker,event_type,status,event_at,
               source_url,payload_json
        FROM public_market_events
        WHERE event_at>?
          AND event_type IN ({placeholders})
          AND NOT (event_type='trading_halt' AND status='resume_announced')
        ORDER BY event_at DESC LIMIT 20
        """,
        (cutoff, *TELEGRAM_EVENT_TYPES),
    ).fetchall()
    cards = []
    for row in rows:
        card = event_card(_channel_event(dict(row), at), origin=origin)
        if card["text"]:
            cards.append(card)
    return enqueue_cards(database, config.chat_id, cards, at=at)


def announcement_history(limit: int = 50) -> dict:
    with connection() as database:
        rows = database.execute(
            "SELECT * FROM telegram_outbox ORDER BY created_at DESC,id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        posts = []
        for row in rows:
            post = dict(row)
            post["items"] = [
                json.loads(r[0])
                for r in database.execute(
                    "SELECT card_json FROM telegram_outbox_items "
                    "WHERE outbox_id=? ORDER BY kind,subject",
                    (row["id"],),
                ).fetchall()
            ]
            post["format"] = "text"
            post["display_text"] = strip_markdown_v2(post["text"])
            posts.append(post)
        for row in database.execute(
            "SELECT coin_id AS id,chat_id,caption_text AS text,status,attempts,message_id,"
            "retry_at,last_error,created_at,updated_at,replay_id FROM memecoin_replay_posts "
            "WHERE status<>'baseline' ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall():
            posts.append(
                {
                    **dict(row),
                    "format": "animation",
                    "items": [],
                    "display_text": strip_markdown_v2(row["text"] or ""),
                }
            )
    return {
        "posts": sorted(posts, key=lambda p: p["created_at"], reverse=True)[:limit],
        "limits": {
            "daily": _setting("TELEGRAM_CHANNEL_DAILY_LIMIT", 24),
            "interval_seconds": _setting(
                "TELEGRAM_CHANNEL_INTERVAL_SECONDS",
                _setting("TELEGRAM_SEGMENT_GAP_MINUTES", 12) * 60,
            ),
            "ticker_quiet_seconds": _setting("TELEGRAM_TICKER_QUIET_SECONDS", 1800),
        },
    }
