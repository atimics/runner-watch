"""Dash's own voice, research interests, and next visit to his desk."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

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
            "review_minutes": {"type": "integer", "minimum": 30, "maximum": 1440},
        },
        "required": ["voice", "focus", "question", "review_minutes"],
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
    minutes = args.get("review_minutes")
    if type(minutes) is not int or not 30 <= minutes <= 1440:
        return {"error": "Choose a review time from 30 to 1440 minutes."}
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
            "review_at": (at + timedelta(minutes=minutes)).isoformat(),
        },
    }


def due(scope, at):
    direction = read(scope)
    with connection() as database:
        row = database.execute(
            "SELECT value FROM worker_state WHERE key=?", (_key(scope, "explored"),)
        ).fetchone()
    last = datetime.fromisoformat(row["value"]) if row else None
    review = datetime.fromisoformat(direction["review_at"]) if direction else None
    if review and (last is None or review > last):
        return at >= review
    return last is None or at >= last + timedelta(hours=EXPLORE_HOURS)


def finish(database, scope, direction, at, *, explored=False):
    """Commit personal notes and desk progress alongside the research result."""
    rows = []
    if direction:
        rows.append((_key(scope, "notes"), json.dumps(direction), direction["updated_at"]))
    if explored:
        rows.append((_key(scope, "explored"), at.isoformat(), at.isoformat()))
    for row in rows:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at "
            "WHERE worker_state.updated_at<=excluded.updated_at",
            row,
        )
