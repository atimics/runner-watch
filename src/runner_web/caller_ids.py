from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from runner_web.db import connection
from runner_web.pseudonyms import ADJECTIVES, ANIMALS


def _iso() -> str:
    return datetime.now(UTC).isoformat()


def _public(row: Any) -> dict[str, Any]:
    return {"id": str(row["id"]), "handle": str(row["handle"])}


def ensure_caller_identity_with_database(database: Any, user_id: str) -> dict[str, Any]:

    if not database.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
        raise KeyError("Account not found")
    existing = database.execute(
        """
        SELECT id,handle FROM caller_identities
        WHERE user_id=? AND status='active'
        ORDER BY claimed_at,id LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    if existing:
        return _public(existing)

    handles = [f"{adjective}-{animal}" for adjective in ADJECTIVES for animal in ANIMALS]
    secrets.SystemRandom().shuffle(handles)
    claimed_at = _iso()
    for handle in handles:
        identity_id = str(uuid.uuid4())
        inserted = database.execute(
            """
            INSERT INTO caller_identities(
                id,handle,user_id,status,claim_cost_cents,claimed_at
            ) VALUES(?,?,?,'active',0,?) ON CONFLICT DO NOTHING
            """,
            (identity_id, handle, user_id, claimed_at),
        )
        if inserted.rowcount == 1:
            return {"id": identity_id, "handle": handle}
        existing = database.execute(
            """
            SELECT id,handle FROM caller_identities
            WHERE user_id=? AND status='active'
            ORDER BY claimed_at,id LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if existing:
            return _public(existing)
    raise RuntimeError("The anonymous caller name space is full")


def ensure_caller_identity(user_id: str) -> dict[str, Any]:

    with connection() as database:
        return ensure_caller_identity_with_database(database, user_id)


MACHINE_USER_ID = "rati-flash"
MACHINE_HANDLE = "rati-flash"
MACHINE_DISPLAY_NAME = "RATi Flash"


def ensure_machine_trader(database: Any = None) -> dict[str, str]:
    """Create or return the machine's system account and public caller identity.

    The machine plays the same day-session Calls game as human traders so its
    record is directly comparable on /u/rati-flash and the public feeds.
    """
    timestamp = _iso()
    if database is None:
        with connection() as database:
            return ensure_machine_trader(database)
    database.execute(
        """
        INSERT INTO users(id,username,display_name,status,created_at)
        VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING
        """,
        (MACHINE_USER_ID, MACHINE_HANDLE, MACHINE_DISPLAY_NAME, "active", timestamp),
    )
    database.execute(
        """
        INSERT INTO caller_identities(
            id,handle,user_id,status,claim_cost_cents,claimed_at
        ) VALUES(?,?,?,'active',0,?) ON CONFLICT DO NOTHING
        """,
        (str(uuid.uuid4()), MACHINE_HANDLE, MACHINE_USER_ID, timestamp),
    )
    row = database.execute(
        "SELECT id,handle FROM caller_identities WHERE handle=? AND status='active'",
        (MACHINE_HANDLE,),
    ).fetchone()
    if not row:
        raise RuntimeError("The machine caller identity could not be created")
    return _public(row)
