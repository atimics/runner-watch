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
    """Return the account's automatic anonymous Call identity."""

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
    """Return one automatic anonymous identity without exposing a picker."""

    with connection() as database:
        return ensure_caller_identity_with_database(database, user_id)
