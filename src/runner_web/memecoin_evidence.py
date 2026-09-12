"""Durable transaction receipts, decoded events, cursors and shared credit budget."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.db import connection

DAILY_CREDIT_CAP = 10_000
RETENTION_DAYS = 30
MAX_TRANSACTIONS = 250_000
PRUNE_BATCH_SIZE = 500
PRUNE_LOCK_ID = 728416203
LOG = logging.getLogger(__name__)


class CreditBudgetReached(ValueError):
    pass


def credit_limit() -> int:
    return max(0, min(int(os.getenv("HELIUS_DAILY_CREDITS", "10000")), DAILY_CREDIT_CAP))


def reserve_credits(credits: int, *, at: datetime | None = None) -> None:
    current = at or datetime.now(UTC)
    limit = credit_limit()
    if type(credits) is not int or credits <= 0 or credits > limit:
        raise CreditBudgetReached("Helius daily credit budget reached")
    with connection() as database:
        reserved = database.execute(
            "INSERT INTO memecoin_helius_budget(day,reserved_credits,request_count,updated_at) "
            "VALUES(?,?,1,?) ON CONFLICT(day) DO UPDATE SET "
            "reserved_credits=memecoin_helius_budget.reserved_credits+excluded.reserved_credits,"
            "request_count=memecoin_helius_budget.request_count+1,updated_at=excluded.updated_at "
            "WHERE memecoin_helius_budget.reserved_credits+excluded.reserved_credits<=? "
            "RETURNING day",
            (current.date().isoformat(), credits, current.isoformat(), limit),
        ).fetchone()
    if reserved is None:
        raise CreditBudgetReached("Helius daily credit budget reached")


def budget_status(*, at: datetime) -> dict[str, Any]:
    with connection() as database:
        row = database.execute(
            "SELECT reserved_credits,request_count FROM memecoin_helius_budget WHERE day=?",
            (at.date().isoformat(),),
        ).fetchone()
    used = row["reserved_credits"] if row else 0
    return {
        "daily_limit": credit_limit(),
        "reserved_credits": used,
        "remaining_credits": max(0, credit_limit() - used),
        "day": at.date().isoformat(),
    }


def stream_state(stream: str) -> dict[str, Any]:
    with connection() as database:
        row = database.execute(
            "SELECT state_json FROM memecoin_chain_cursors WHERE stream=?", (stream,)
        ).fetchone()
    return json.loads(row["state_json"]) if row else {}


def save_batch(
    stream: str,
    entries: list[dict[str, Any]],
    events: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    at: datetime,
) -> None:
    """Commit receipts and the next cursor together so restart replay is safe."""
    with connection() as database:
        for entry in entries:
            signature = entry["transaction"]["signatures"][0]
            observed = datetime.fromtimestamp(entry["blockTime"], UTC).isoformat()
            # Keep the fields used by the parsers; omit provider decoration.
            evidence = {
                field: entry[field] for field in ("transaction", "meta", "slot", "blockTime")
            }
            encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if len(encoded.encode()) > 1024 * 1024:
                raise ValueError("Transaction receipt exceeds the storage limit")
            database.execute(
                "INSERT INTO memecoin_chain_transactions(signature,slot,observed_at,evidence_json,"
                "evidence_sha256,collected_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(signature) DO NOTHING",
                (
                    signature,
                    entry["slot"],
                    observed,
                    encoded,
                    hashlib.sha256(encoded.encode()).hexdigest(),
                    at.isoformat(),
                ),
            )
        for event in events:
            database.execute(
                "INSERT INTO memecoin_chain_events(event_id,kind,token_address,wallet,pool_address,"
                "observed_at,signature,evidence_json) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(event_id) DO NOTHING",
                (
                    event["event_id"],
                    event["kind"],
                    event.get("token_address"),
                    event.get("wallet"),
                    event.get("pool_address"),
                    event["observed_at"],
                    event["signature"],
                    json.dumps(event, sort_keys=True, allow_nan=False),
                ),
            )
        for gap in state.get("coverage_gaps", []):
            database.execute(
                "INSERT INTO memecoin_chain_gaps(stream,start_time,end_time,reason,recorded_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(stream,start_time,end_time) DO NOTHING",
                (stream, gap["start"], gap["end"], gap["reason"], at.isoformat()),
            )
        database.execute(
            "INSERT INTO memecoin_chain_cursors(stream,state_json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(stream) DO UPDATE SET state_json=excluded.state_json,"
            "updated_at=excluded.updated_at "
            "WHERE memecoin_chain_cursors.updated_at<=excluded.updated_at",
            (stream, json.dumps(state, allow_nan=False), at.isoformat()),
        )


def recent_events(
    *, at: datetime, kind: str | None = None, limit: int = 5000
) -> list[dict[str, Any]]:
    params = [(at - timedelta(days=RETENTION_DAYS)).isoformat()]
    clause = ""
    if kind:
        clause = " AND kind=?"
        params.append(kind)
    params.append(max(1, min(limit, 10000)))
    with connection() as database:
        rows = database.execute(
            "SELECT evidence_json FROM memecoin_chain_events WHERE observed_at>=?"
            + clause
            + " ORDER BY observed_at DESC,event_id LIMIT ?",
            tuple(params),
        ).fetchall()
    return [json.loads(row["evidence_json"]) for row in rows]


def evidence_status(*, at: datetime) -> dict[str, Any]:
    with connection() as database:
        counts = database.execute(
            "SELECT kind,COUNT(*) AS count FROM memecoin_chain_events GROUP BY kind"
        ).fetchall()
        gap_count = database.execute("SELECT COUNT(*) FROM memecoin_chain_gaps").fetchone()[0]
        streams = database.execute(
            "SELECT stream,state_json,updated_at FROM memecoin_chain_cursors"
        ).fetchall()
    return {
        "event_counts": {row["kind"]: row["count"] for row in counts},
        "recorded_coverage_gaps": gap_count,
        "streams": [
            {
                **json.loads(row["state_json"]),
                "stream": row["stream"],
                "checked_at": row["updated_at"],
            }
            for row in streams
        ],
        "budget": budget_status(at=at),
    }


def prune_evidence(*, at: datetime) -> None:
    """Trim a bounded batch using indexed receipt keys and one PostgreSQL worker."""
    from psycopg.errors import LockNotAvailable, QueryCanceled

    oldest = (at - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        with connection() as database:
            if database.backend == "postgres":
                database.execute("SET LOCAL statement_timeout = '5s'")
                database.execute("SET LOCAL lock_timeout = '1s'")
                acquired = database.execute(
                    "SELECT pg_try_advisory_xact_lock(?) AS acquired", (PRUNE_LOCK_ID,)
                ).fetchone()
                if not acquired["acquired"]:
                    return
            database.execute(
                "DELETE FROM memecoin_chain_gaps WHERE (stream,start_time,end_time) IN "
                "(SELECT stream,start_time,end_time FROM memecoin_chain_gaps "
                "WHERE recorded_at<? ORDER BY recorded_at LIMIT ?)",
                (oldest, PRUNE_BATCH_SIZE),
            )
            database.execute(
                "DELETE FROM memecoin_chain_events WHERE event_id IN "
                "(SELECT event_id FROM memecoin_chain_events WHERE observed_at<? "
                "ORDER BY observed_at,event_id LIMIT ?)",
                (oldest, PRUNE_BATCH_SIZE),
            )
            expired = database.execute(
                "SELECT signature FROM memecoin_chain_transactions WHERE observed_at<? "
                "ORDER BY observed_at,signature LIMIT ?",
                (oldest, PRUNE_BATCH_SIZE),
            ).fetchall()
            _delete_receipts(database, [row["signature"] for row in expired])
            remaining = PRUNE_BATCH_SIZE - len(expired)
            if remaining:
                overflow = database.execute(
                    "SELECT signature FROM memecoin_chain_transactions "
                    "ORDER BY observed_at DESC,signature DESC LIMIT ? OFFSET ?",
                    (remaining, MAX_TRANSACTIONS),
                ).fetchall()
                _delete_receipts(database, [row["signature"] for row in overflow])
    except (LockNotAvailable, QueryCanceled):
        LOG.warning("Coin evidence cleanup deferred after its database time limit")


def _delete_receipts(database: Any, signatures: list[str]) -> None:
    if not signatures:
        return
    placeholders = ",".join("?" for _ in signatures)
    # Delete each receipt's decoded events in the same transaction as its receipt.
    database.execute(
        f"DELETE FROM memecoin_chain_events WHERE signature IN ({placeholders})", signatures
    )
    database.execute(
        f"DELETE FROM memecoin_chain_transactions WHERE signature IN ({placeholders})", signatures
    )


def transaction_receipt(signature: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            "SELECT evidence_json,evidence_sha256,collected_at FROM memecoin_chain_transactions "
            "WHERE signature=?",
            (signature,),
        ).fetchone()
    if row is None:
        return None
    return {
        "transaction": json.loads(row["evidence_json"]),
        "sha256": row["evidence_sha256"],
        "collected_at": row["collected_at"],
    }
