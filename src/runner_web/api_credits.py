"""API keys, durable credit reservations, and bounded request receipts."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from runner_web.db import connection

FREE_MONTH = 1000
FREE_DAY = 100
PRICES = {
    "snapshot": 1,
    "tokens": 5,
    "analysis": 10,
    "wallet": 10,
    "history": 25,
    "token_check": 25,
    "wallet_scan": 100,
}
PACKS = {1: 1000, 5: 5000, 20: 20000}


def stamp() -> str:
    return datetime.now(UTC).isoformat()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def create_key(user_id: str, name: str) -> dict[str, str]:
    value = "rw_" + secrets.token_urlsafe(32)
    identifier = secrets.token_hex(12)
    with connection() as db:
        account(db, user_id)
        count = db.execute(
            "SELECT COUNT(*) AS n FROM api_keys WHERE user_id=? AND revoked_at IS NULL", (user_id,)
        ).fetchone()["n"]
        if count >= 5:
            raise HTTPException(409, "Each account can have five active API keys.")
        db.execute(
            "INSERT INTO api_keys(id,user_id,name,key_hash,created_at) VALUES(?,?,?,?,?)",
            (identifier, user_id, name, digest(value), stamp()),
        )
    return {"id": identifier, "name": name, "key": value}


def authenticate(value: str) -> str:
    if not value.startswith("Bearer rw_") or len(value) > 200:
        raise HTTPException(401, "Use your API key as a Bearer token.")
    with connection() as db:
        row = db.execute(
            "SELECT k.user_id FROM api_keys k JOIN users u ON u.id=k.user_id "
            "WHERE k.key_hash=? AND k.revoked_at IS NULL AND u.status='active'",
            (digest(value[7:]),),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Create an active API key at /developers.")
    return str(row["user_id"])


def account(db: Any, user_id: str, at: datetime | None = None) -> dict[str, Any]:
    now = at or datetime.now(UTC)
    month, day = now.strftime("%Y-%m"), now.date().isoformat()
    db.execute(
        "INSERT INTO api_accounts(user_id,month,day) VALUES(?,?,?) ON CONFLICT DO NOTHING",
        (user_id, month, day),
    )
    # This write serializes reservations, refunds, and top-ups across all web processes.
    db.execute(
        "UPDATE api_accounts SET free_month_used=CASE WHEN month=? THEN free_month_used ELSE 0 END,"
        "free_day_used=CASE WHEN day=? THEN free_day_used ELSE 0 END,month=?,day=? WHERE user_id=?",
        (month, day, month, day, user_id),
    )
    return dict(db.execute("SELECT * FROM api_accounts WHERE user_id=?", (user_id,)).fetchone())


def balance(user_id: str, at: datetime | None = None) -> dict[str, Any]:
    with connection() as db:
        row = account(db, user_id, at)
    free = max(0, min(FREE_MONTH - row["free_month_used"], FREE_DAY - row["free_day_used"]))
    return {
        "paid_credits": row["paid_credits"],
        "free_credits_available": free,
        "free_month_remaining": max(0, FREE_MONTH - row["free_month_used"]),
        "free_day_remaining": max(0, FREE_DAY - row["free_day_used"]),
        "tier": "paid" if row["paid_credits"] > 0 else "free",
        "month": row["month"],
        "day": row["day"],
    }


def reserve(
    user_id: str,
    key: str,
    operation: str,
    parameters: dict[str, Any],
    *,
    paid_only: bool = False,
    at: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    now = at or datetime.now(UTC)
    identity = digest(user_id + ":" + key)
    fingerprint = digest(json.dumps([operation, parameters], sort_keys=True, separators=(",", ":")))
    cost = PRICES[operation]
    with connection() as db:
        row = account(db, user_id, now)
        previous = db.execute("SELECT * FROM api_requests WHERE id=?", (identity,)).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise HTTPException(409, "Use a new Idempotency-Key for different request details.")
            return dict(previous), False
        if paid_only:
            busy = db.execute(
                "SELECT id FROM api_requests WHERE user_id=? "
                "AND status IN ('queued','running') LIMIT 1",
                (user_id,),
            ).fetchone()
            if busy:
                raise HTTPException(
                    429, "Your current job is still running.", headers={"Retry-After": "10"}
                )
        free = (
            0
            if paid_only
            else min(cost, FREE_MONTH - row["free_month_used"], FREE_DAY - row["free_day_used"])
        )
        paid = cost - free
        if row["paid_credits"] < paid:
            raise HTTPException(
                402,
                {
                    "message": "Add API credits to continue.",
                    "cost": cost,
                    "available": row["paid_credits"] + free,
                    "topup_url": "/api/v1/topups",
                },
            )
        db.execute(
            "UPDATE api_accounts SET paid_credits=paid_credits-?,free_month_used=free_month_used+?,"
            "free_day_used=free_day_used+? WHERE user_id=?",
            (paid, free, free, user_id),
        )
        db.execute(
            "INSERT INTO api_requests(id,user_id,fingerprint,operation,parameters,status,"
            "free_cost,paid_cost,month,day,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                user_id,
                fingerprint,
                operation,
                json.dumps(parameters),
                "queued" if paid_only else "reserved",
                free,
                paid,
                row["month"],
                row["day"],
                now.isoformat(),
                now.isoformat(),
            ),
        )
        return dict(
            db.execute("SELECT * FROM api_requests WHERE id=?", (identity,)).fetchone()
        ), True


def finish(
    identifier: str, *, result: dict[str, Any] | None = None, error: str | None = None
) -> None:
    with connection() as db:
        row = db.execute("SELECT * FROM api_requests WHERE id=?", (identifier,)).fetchone()
        if row is None:
            return
        account(db, row["user_id"])
        updated = db.execute(
            "UPDATE api_requests SET status=?,result_json=?,error=?,updated_at=? "
            "WHERE id=? AND status IN ('reserved','queued','running')",
            (
                "failed" if error else "complete",
                json.dumps(result) if result else None,
                error,
                stamp(),
                identifier,
            ),
        )
        if updated.rowcount and error:
            db.execute(
                "UPDATE api_accounts SET paid_credits=paid_credits+?,"
                "free_month_used=free_month_used-CASE WHEN month=? THEN ? ELSE 0 END,"
                "free_day_used=free_day_used-CASE WHEN day=? THEN ? ELSE 0 END "
                "WHERE user_id=?",
                (
                    row["paid_cost"],
                    row["month"],
                    row["free_cost"],
                    row["day"],
                    row["free_cost"],
                    row["user_id"],
                ),
            )


def receipt(identifier: str, user_id: str) -> dict[str, Any]:
    with connection() as db:
        row = db.execute(
            "SELECT * FROM api_requests WHERE id=? AND user_id=?", (identifier, user_id)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Request receipt has expired or the ID is unknown.")
    return dict(row)


def prune() -> None:
    now = datetime.now(UTC)
    with connection() as db:
        stuck = db.execute(
            "SELECT id FROM api_requests WHERE status IN ('reserved','queued','running') "
            "AND updated_at<? ORDER BY updated_at LIMIT 100",
            ((now - timedelta(minutes=15)).isoformat(),),
        ).fetchall()
    for row in stuck:
        finish(row["id"], error="The job timed out. Its credits have been returned.")
    with connection() as db:
        db.execute(
            "DELETE FROM api_requests WHERE id IN (SELECT id FROM api_requests "
            "WHERE status IN ('complete','failed') AND updated_at<? ORDER BY updated_at LIMIT 1000)",
            ((now - timedelta(days=7)).isoformat(),),
        )
        db.execute(
            "DELETE FROM api_provider_budget WHERE day<?",
            ((now - timedelta(days=30)).date().isoformat(),),
        )
