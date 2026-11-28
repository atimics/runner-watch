"""Durable research checkpoints and atomic handoff to Telegram's outbox."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from runner_web.db import connection
from runner_web.telegram_outbox import enqueue_cards, lock_channel

MAX_ATTEMPTS = 3


def desk_card(database, scope, identity, chat_id, result, at):
    card = {
        "kind": "dash_research",
        "subject": f"{scope}:{identity}",
        "text": result["text"],
        "research_job": identity,
    }
    if not (story := result.get("story")):
        return card
    topic = hashlib.sha256(story["node"].encode()).hexdigest()[:24]
    prefix = f"{scope}:story:{topic}:"
    card["subject"] = prefix + story["version"]
    parent = database.execute(
        "SELECT o.message_id FROM telegram_outbox o "
        "JOIN telegram_outbox_items i ON i.outbox_id=o.id "
        "WHERE o.chat_id=? AND o.status='sent' AND o.message_id IS NOT NULL AND o.created_at>=? "
        "AND i.kind='dash_research' AND i.subject LIKE ? AND i.subject<>? "
        "ORDER BY o.created_at,o.id LIMIT 1",
        (str(chat_id), (at - timedelta(days=7)).isoformat(), prefix + "%", card["subject"]),
    ).fetchone()
    if parent:
        card["reply_to_message_id"] = parent["message_id"]
    else:
        # A direct room answer may have opened the story and saved the review.
        replies = database.execute(
            "SELECT result_json FROM dash_research_jobs WHERE scope=? AND kind='chat' "
            "AND status='complete' AND updated_at>=? ORDER BY updated_at DESC LIMIT 100",
            (scope, (at - timedelta(days=7)).isoformat()),
        ).fetchall()
        for reply in replies:
            earlier = json.loads(reply["result_json"])
            delivery = earlier.get("delivery", {})
            if earlier.get("story", {}).get("node") == story["node"] and (
                delivery.get("status") == "sent" and delivery.get("message_id")
            ):
                card["reply_to_message_id"] = delivery["message_id"]
                break
    return card


def active_desk_key(scope):
    with connection() as database:
        row = database.execute(
            "SELECT id FROM dash_research_jobs WHERE scope=? AND kind='desk' "
            "AND status='pending' ORDER BY created_at LIMIT 1",
            (scope,),
        ).fetchone()
    return row["id"] if row else None


def completed_result(identity, scope):
    with connection() as database:
        row = database.execute(
            "SELECT result_json FROM dash_research_jobs "
            "WHERE id=? AND scope=? AND status='complete'",
            (identity, scope),
        ).fetchone()
    return json.loads(row["result_json"]) if row else None


def record_delivery(database, identity, status, at, *, message_id=None):
    """Keep answer quality and Telegram acknowledgement separate."""
    row = database.execute(
        "SELECT result_json FROM dash_research_jobs WHERE id=? AND status='complete'",
        (identity,),
    ).fetchone()
    if not row:
        return
    result = json.loads(row["result_json"])
    result["delivery"] = {
        "status": status,
        "completed_at": at.isoformat(),
        "message_id": message_id,
    }
    database.execute(
        "UPDATE dash_research_jobs SET result_json=? WHERE id=? AND status='complete'",
        (json.dumps(result), identity),
    )


def claim(identity, scope, kind, at):
    token = uuid4().hex
    stamp = at.isoformat()
    with connection() as database:
        database.execute(
            "UPDATE dash_research_jobs SET status='complete',state_json='{}',result_json=?,"
            "updated_at=? WHERE status='pending' AND created_at<? AND lease_until<=?",
            (
                json.dumps(
                    {
                        "action": "hold",
                        "why": "research_expired",
                        "research": {
                            "status": "incomplete",
                            "outcome": "incomplete",
                            "stop_reason": "research_expired",
                            "completed_at": stamp,
                        },
                    }
                ),
                stamp,
                (at - timedelta(days=1)).isoformat(),
                stamp,
            ),
        )
        database.execute(
            "DELETE FROM dash_research_jobs WHERE updated_at<? AND status='complete'",
            ((at - timedelta(days=14)).isoformat(),),
        )
        database.execute(
            "INSERT INTO dash_research_jobs(id,scope,kind,status,created_at,updated_at) "
            "VALUES(?,?,?,'pending',?,?) ON CONFLICT DO NOTHING",
            (identity, scope, kind, stamp, stamp),
        )
        row = database.execute(
            "SELECT * FROM dash_research_jobs WHERE id=? AND scope=?",
            (identity, scope),
        ).fetchone()
        if not row:
            return None
        if row["status"] == "complete":
            return {"result": json.loads(row["result_json"])}
        claimed = database.execute(
            "UPDATE dash_research_jobs SET lease_token=?,lease_until=?,attempts=attempts+1,"
            "updated_at=? WHERE id=? AND status='pending' AND next_at<=? AND lease_until<=? "
            "RETURNING state_json,attempts",
            (token, (at + timedelta(minutes=5)).isoformat(), stamp, identity, stamp, stamp),
        ).fetchone()
        if not claimed:
            return None
    return {
        "token": token,
        "state": json.loads(claimed["state_json"]),
        "attempts": claimed["attempts"],
        "created_at": row["created_at"],
    }


def checkpoint(identity, token, state, at):
    with connection() as database:
        row = database.execute(
            "UPDATE dash_research_jobs SET state_json=?,updated_at=? "
            "WHERE id=? AND lease_token=? AND status='pending' RETURNING id",
            (
                json.dumps(state, separators=(",", ":"), default=str),
                at.isoformat(),
                identity,
                token,
            ),
        ).fetchone()
        if not row:
            raise RuntimeError("Research lease changed")


def release(identity, token, at):
    with connection() as database:
        database.execute(
            "UPDATE dash_research_jobs SET lease_until='',lease_token='',next_at=?,updated_at=? "
            "WHERE id=? AND lease_token=? AND status='pending'",
            ((at + timedelta(seconds=60)).isoformat(), at.isoformat(), identity, token),
        )


def finish(
    identity, token, scope, result, notes, at, *, chat_id=None, direction=None, explored=False
):
    from runner_web import dash_direction, dash_schedule
    from runner_web.dash_research import store_note

    with connection() as database:
        row = database.execute(
            "UPDATE dash_research_jobs SET status='complete',result_json=?,state_json='{}',"
            "lease_until='',lease_token='',updated_at=? WHERE id=? AND lease_token=? "
            "AND status='pending' RETURNING id",
            (json.dumps(result), at.isoformat(), identity, token),
        ).fetchone()
        if not row:
            raise RuntimeError("Research lease changed")
        card = None
        if chat_id is not None and result["action"] == "reply":
            if result.get("story"):
                lock_channel(database, str(chat_id), at)
            card = desk_card(database, scope, identity, chat_id, result, at)
            if (
                result.get("story")
                and database.execute(
                    "SELECT 1 FROM telegram_outbox_items WHERE kind=? AND subject=?",
                    (card["kind"], card["subject"]),
                ).fetchone()
            ):
                result.pop("_schedule", None)
                result["delivery"] = {"status": "duplicate"}
                card = None
        announced = result.get("_schedule", {}).get("references", [])
        # Complete existing reviews before additions claim their bounded notebook slots.
        for note in sorted(notes.values(), key=lambda item: not item.get("_base_revision")):
            store_note(
                database,
                scope,
                note,
                at,
                announced=note["node"] in announced,
                retain_nodes=announced,
            )
        dash_direction.finish(
            database,
            scope,
            direction,
            at,
            explored=explored,
            announced="direction" in announced,
            failed=result.get("research", {}).get("status") == "incomplete",
            explored_at=dash_schedule.stored_instant(
                result.get("research", {}).get("attempt_started_at")
            ),
        )
        dash_schedule.finalize(database, scope, result, at)
        database.execute(
            "UPDATE dash_research_jobs SET result_json=? WHERE id=? AND status='complete'",
            (json.dumps(result), identity),
        )
        if card:
            card["text"] = result["text"]
            enqueue_cards(
                database,
                str(chat_id),
                [card],
                at=at,
            )
