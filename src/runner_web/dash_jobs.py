"""Durable research checkpoints and atomic handoff to Telegram's outbox."""

from __future__ import annotations

import json
from datetime import timedelta
from uuid import uuid4

from runner_web.db import connection
from runner_web.telegram_outbox import enqueue_cards

MAX_ATTEMPTS = 3


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


def claim(identity, scope, kind, at):
    token = uuid4().hex
    stamp = at.isoformat()
    with connection() as database:
        database.execute(
            "UPDATE dash_research_jobs SET status='complete',state_json='{}',result_json=?,"
            "updated_at=? WHERE status='pending' AND created_at<? AND lease_until<=?",
            (
                json.dumps({"action": "hold", "why": "research_expired"}),
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
            "UPDATE dash_research_jobs SET lease_until='',lease_token='',next_at=? "
            "WHERE id=? AND lease_token=? AND status='pending'",
            ((at + timedelta(seconds=60)).isoformat(), identity, token),
        )


def finish(identity, token, scope, result, notes, at, *, chat_id=None):
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
        for note in notes.values():
            store_note(database, scope, note, at)
        if chat_id is not None and result["action"] == "reply":
            enqueue_cards(
                database,
                str(chat_id),
                [
                    {
                        "kind": "dash_research",
                        "subject": f"{scope}:{identity}",
                        "text": result["text"],
                        "research_job": identity,
                    }
                ],
                at=at,
            )
