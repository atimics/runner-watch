"""Dash's own voice, research interests, and next visit to his desk."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from runner_web import dash_schedule
from runner_web.db import connection

EXPLORE_HOURS = 6

DIRECTION_TOOL = {
    "name": "set_direction",
    "description": (
        "Write or revise your own voice, research focus, and next question for this chat "
        "and topic. Use your own words. Choose when to return, from 30 minutes to one day. "
        "The main room's desk worker follows this agenda; other chats use it on later turns. "
        "These are personal working notes that you can revise as you learn."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "voice": {"type": "string", "maxLength": 500},
            "focus": {"type": "string", "maxLength": 700},
            "question": {"type": "string", "maxLength": 500},
            **dash_schedule.TIME_PROPERTIES,
        },
        "required": ["voice", "focus", "question"],
        "anyOf": [{"required": ["review_at"]}, {"required": ["review_minutes"]}],
    },
}


def _key(scope, kind):
    return "dash_direction:" + hashlib.sha256(scope.encode()).hexdigest()[:24] + ":" + kind


def read(scope):
    with connection() as database:
        row = database.execute(
            "SELECT value FROM worker_state WHERE key=?", (_key(scope, "notes"),)
        ).fetchone()
    return json.loads(row["value"]) if row else {}


def propose(args, at):
    try:
        schedule = dash_schedule.propose(args, at)
    except ValueError as exc:
        return {"error": str(exc)}
    fields = {}
    for name, limit in (("voice", 500), ("focus", 700), ("question", 500)):
        value = args.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            return {"error": f"Write {name} in 1 to {limit} characters."}
        fields[name] = value.strip()
    return {
        "saved": True,
        "direction": {
            **fields,
            "updated_at": at.isoformat(),
            **schedule,
        },
        "schedule": dash_schedule.reference("direction", schedule),
    }


def next_review(scope):
    direction = read(scope)
    with connection() as database:
        rows = database.execute(
            "SELECT key,value FROM worker_state WHERE key IN (?,?)",
            (_key(scope, "explored"), _key(scope, "retry_at")),
        ).fetchall()
    values = {row["key"]: row["value"] for row in rows}
    last = dash_schedule.stored_instant(values.get(_key(scope, "explored")))
    retry = dash_schedule.stored_instant(values.get(_key(scope, "retry_at")))
    review = dash_schedule.stored_instant(direction.get("review_at"))
    if review and (last is None or review > last):
        return max(review, retry) if retry else review
    next_at = last + timedelta(hours=EXPLORE_HOURS) if last else None
    return max(next_at, retry) if next_at and retry else next_at or retry


def due(scope, at):
    review = next_review(scope)
    return review is None or at >= review


def finish(
    database,
    scope,
    direction,
    at,
    *,
    explored=False,
    announced=False,
    explored_at=None,
    failed=False,
):
    """Commit personal notes and desk progress alongside the research result."""
    rows = []
    if direction and not failed:
        prior = database.execute(
            "SELECT value FROM worker_state WHERE key=?", (_key(scope, "notes"),)
        ).fetchone()
        if prior:
            direction = dash_schedule.preserve_promise(
                json.loads(prior["value"]), direction, at, announced=announced
            )
        rows.append((_key(scope, "notes"), json.dumps(direction), direction["updated_at"]))
    if explored:
        if failed:
            rows.append(
                (_key(scope, "retry_at"), (at + timedelta(minutes=30)).isoformat(), at.isoformat())
            )
        else:
            rows.append((_key(scope, "explored"), (explored_at or at).isoformat(), at.isoformat()))
            database.execute(
                "DELETE FROM worker_state WHERE key=? AND updated_at<=?",
                (_key(scope, "retry_at"), at.isoformat()),
            )
    for row in rows:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at "
            "WHERE worker_state.updated_at<=excluded.updated_at",
            row,
        )
