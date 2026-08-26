from __future__ import annotations

import os
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from runner_web.db import connection
from runner_web.pseudonyms import ADJECTIVES, ANIMALS

CALLER_ID_CLAIM_PRICE_CENTS = max(
    1, int(os.getenv("CALLER_ID_CLAIM_PRICE_CENTS", "500"))
)


class AdditionalCallerIdPaymentRequired(ValueError):
    """Raised when an account has already used its free caller-ID claim."""


def _iso() -> str:
    return datetime.now(UTC).isoformat()


def _public(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "handle": str(row["handle"]),
        "status": str(row["status"]),
        "claim_cost_cents": int(row["claim_cost_cents"] or 0),
        "claimed_at": str(row["claimed_at"]),
    }


def caller_ids_for_user(user_id: str) -> list[dict[str, Any]]:
    """Return active caller identities without exposing the owning account publicly."""

    with connection() as database:
        rows = database.execute(
            """
            SELECT id,handle,status,claim_cost_cents,claimed_at
            FROM caller_identities
            WHERE user_id=? AND status='active'
            ORDER BY claimed_at,id
            """,
            (user_id,),
        ).fetchall()
    return [_public(row) for row in rows]


def claim_caller_id(
    user_id: str,
    *,
    payment_reference: str | None = None,
) -> dict[str, Any]:
    """Claim a random animal identity; the first claim is free and later claims are paid."""

    with connection() as database:
        return claim_caller_id_with_database(
            database,
            user_id,
            payment_reference=payment_reference,
        )


def claim_caller_id_with_database(
    database: Any,
    user_id: str,
    *,
    payment_reference: str | None = None,
) -> dict[str, Any]:
    """Claim inside an existing transaction, including a verified payment webhook."""

    clean_reference = str(payment_reference or "").strip() or None
    if not database.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
        raise KeyError("Account not found")
    claims = int(
        database.execute(
            "SELECT COUNT(*) FROM caller_identity_claims WHERE user_id=?",
            (user_id,),
        ).fetchone()[0]
    )
    if claims > 0 and not clean_reference:
        raise AdditionalCallerIdPaymentRequired(
            "The first caller ID is free; another claim needs payment."
        )
    if clean_reference:
        existing = database.execute(
            """
            SELECT i.id,i.handle,i.status,i.claim_cost_cents,
                   COALESCE(i.claimed_at,c.claimed_at) AS claimed_at,c.user_id
            FROM caller_identity_claims c
            LEFT JOIN caller_identities i ON i.id=c.caller_identity_id
            WHERE c.payment_reference=?
            """,
            (clean_reference,),
        ).fetchone()
        if existing:
            if str(existing["user_id"]) != user_id:
                raise ValueError("Payment reference was already used")
            if existing["id"]:
                return _public(existing)
            return {
                "id": "",
                "handle": "",
                "status": "tombstoned",
                "claim_cost_cents": CALLER_ID_CLAIM_PRICE_CENTS,
                "claimed_at": str(existing["claimed_at"]),
            }

    claim_cost = 0 if claims == 0 else CALLER_ID_CLAIM_PRICE_CENTS
    free_claim = 1 if claims == 0 else 0
    claimed_at = _iso()
    handles = [f"{adjective}-{animal}" for adjective in ADJECTIVES for animal in ANIMALS]
    secrets.SystemRandom().shuffle(handles)
    for handle in handles:
        identity_id = str(uuid.uuid4())
        inserted = database.execute(
            """
            INSERT INTO caller_identities(
                id,handle,user_id,status,claim_cost_cents,payment_reference,claimed_at
            ) VALUES(?,?,?,'active',?,?,?)
            ON CONFLICT DO NOTHING
            """,
            (
                identity_id,
                handle,
                user_id,
                claim_cost,
                clean_reference,
                claimed_at,
            ),
        )
        if inserted.rowcount == 0:
            continue
        database.execute(
            """
            INSERT INTO caller_identity_claims(
                id,user_id,caller_identity_id,payment_reference,
                claim_cost_cents,free_claim,claimed_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                user_id,
                identity_id,
                clean_reference,
                claim_cost,
                free_claim,
                claimed_at,
            ),
        )
        row = database.execute(
            """
            SELECT id,handle,status,claim_cost_cents,claimed_at
            FROM caller_identities WHERE id=?
            """,
            (identity_id,),
        ).fetchone()
        return _public(row)
    raise RuntimeError("The caller-ID name space is full")


def delete_caller_id(user_id: str, caller_identity_id: str) -> dict[str, Any]:
    """Erase an identity's calls and ownership while reserving its public name forever."""

    deleted_at = _iso()
    with connection() as database:
        row = database.execute(
            """
            SELECT id,handle FROM caller_identities
            WHERE id=? AND user_id=? AND status='active'
            """,
            (caller_identity_id, user_id),
        ).fetchone()
        if not row:
            return {"deleted": False}
        database.execute(
            "DELETE FROM community_calls WHERE caller_identity_id=?",
            (caller_identity_id,),
        )
        database.execute(
            "UPDATE caller_identity_claims SET caller_identity_id=NULL "
            "WHERE caller_identity_id=?",
            (caller_identity_id,),
        )
        database.execute(
            """
            UPDATE caller_identities
            SET user_id=NULL,status='tombstoned',claim_cost_cents=NULL,
                payment_reference=NULL,claimed_at=NULL,deleted_at=?
            WHERE id=?
            """,
            (deleted_at, caller_identity_id),
        )
    return {"deleted": True, "handle": str(row["handle"])}
