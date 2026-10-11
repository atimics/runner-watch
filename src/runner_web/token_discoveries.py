"""Token submissions, admission to the watch, and one Flash reward per mint."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import ratitrust
from ratitrust.memecoin import LABELS
from runner_web.db import connection
from runner_web.flash_wallet import PUBLISH_REPORT_REWARD, credit_flash
from runner_web.helius_discovery import _decode
from runner_web.ratification_records import fingerprint, rules_digest
from runner_web.token_rejections import append, canonical, checkpoint

DISCOVERY_REWARD = PUBLISH_REPORT_REWARD
WRITE_LOCK = 721906102


def valid_mint(value: str) -> bool:
    return (
        bool(re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", value)) and len(_decode(value, 44)) == 32
    )


def coin_id(mint: str) -> str:
    return "chain-" + hashlib.sha256(f"solana:{mint}".encode()).hexdigest()


def _lock(database: Any) -> None:
    if database.backend == "postgres":
        database.execute("SELECT pg_advisory_xact_lock(?)", (WRITE_LOCK,))
    else:
        database.execute("BEGIN IMMEDIATE")


def _remember(database: Any, mint: str, at: str) -> None:
    # Assets collected before the submission flow keep their existing provenance.
    known = (
        database.execute(
            "SELECT 1 FROM memecoin_assets WHERE coin_id=?", (coin_id(mint),)
        ).fetchone()
        or database.execute(
            "SELECT 1 FROM ratification_records WHERE market='memecoin' AND subject=? LIMIT 1",
            (mint,),
        ).fetchone()
    )
    database.execute(
        "INSERT INTO token_discoveries(mint,status,first_seen_at,reward_eligible) "
        "VALUES(?,'review',?,?) ON CONFLICT DO NOTHING",
        (mint, at, 0 if known else 1),
    )


def remember(mint: str, *, at: datetime | None = None) -> None:
    if not valid_mint(mint):
        raise ValueError("Enter a Solana token mint")
    with connection() as database:
        _lock(database)
        _remember(database, mint, (at or datetime.now(UTC)).isoformat())


def _payload(row: Any, user_id: str | None = None) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row["result_json"] else {}
    yours = bool(user_id and row["submitter_id"] == user_id)
    reward = (
        "paid"
        if yours and row["rewarded_at"]
        else "reserved"
        if yours and row["claimed_at"]
        else "claimed"
        if row["claimed_at"]
        else "available"
        if row["reward_eligible"] and row["status"] != "tracked"
        else "recorded"
    )
    return {
        "mint": row["mint"],
        "status": row["status"],
        "first_seen_at": row["first_seen_at"],
        "checked_at": row["checked_at"],
        "met": result.get("met", 0),
        "total": result.get("total", len(LABELS)),
        "failed_checks": [s for s in result.get("standards", []) if s.get("met") is False],
        "pending_checks": [s for s in result.get("standards", []) if s.get("met") is None],
        "reward_status": reward,
        "reward_amount": row["reward_amount"] if yours else DISCOVERY_REWARD,
        "rewarded_at": row["rewarded_at"] if yours else None,
        "proof_url": f"/api/memecoin-rejections/{row['rejection_index']}"
        if row["rejection_index"] is not None
        else None,
    }


def discovery(mint: str, *, user_id: str | None = None) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute("SELECT * FROM token_discoveries WHERE mint=?", (mint,)).fetchone()
        return {**_payload(row, user_id), "rejection_tree": checkpoint(database)} if row else None


def submit(mint: str, user_id: str, *, at: datetime | None = None) -> dict[str, Any]:
    if not valid_mint(mint):
        raise ValueError("Enter a Solana token mint")
    current = (at or datetime.now(UTC)).isoformat()
    with connection() as database:
        _lock(database)
        _remember(database, mint, current)
        database.execute(
            "UPDATE token_discoveries SET submitter_id=?,claimed_at=?,reward_amount=? "
            "WHERE mint=? AND claimed_at IS NULL AND reward_eligible=1 AND status<>'tracked'",
            (user_id, current, DISCOVERY_REWARD, mint),
        )
        return _payload(
            database.execute("SELECT * FROM token_discoveries WHERE mint=?", (mint,)).fetchone(),
            user_id,
        )


def settle(rows: list[dict[str, Any]], facts: dict[str, Any], *, at: datetime) -> None:
    """Only the worker calls this, with its current RATi Rules readings."""
    stamp = at.isoformat()
    with connection() as database:
        _lock(database)
        for row in rows:
            mint = row.get("token_address")
            saved = database.execute(
                "SELECT * FROM token_discoveries WHERE mint=?", (mint,)
            ).fetchone()
            if saved is None or mint not in facts:
                continue
            result = row.get("ratification") or {}
            checks = result.get("standards") or []
            # Admission uses the full current rule set. Pending readings stay under review.
            passed = (
                result.get("ratified") is True
                and len(checks) == len(LABELS)
                and {s.get("key") for s in checks} == set(LABELS)
                and all(s.get("met") is True and s.get("applies") is True for s in checks)
            )
            failed = [s["key"] for s in checks if s.get("met") is False]
            status = "tracked" if passed else "rejected" if failed else "review"
            mark = hashlib.sha256((fingerprint(result) + rules_digest()).encode()).hexdigest()
            rejection = saved["rejection_index"]
            if status == "rejected" and mark != saved["fingerprint"]:
                evidence = json.loads(
                    json.dumps({"result": result, "facts": facts[mint]}, default=str)
                )
                leaf = {
                    "schema": "rati-token-rejection-v1",
                    "chain": "solana",
                    "mint": mint,
                    "rules_version": ratitrust.__version__,
                    "rules_digest": rules_digest(),
                    "failed_checks": sorted(failed),
                    "recorded_at": stamp,
                    "evidence_sha256": hashlib.sha256(canonical(evidence).encode()).hexdigest(),
                }
                rejection = append(database, mint, leaf, evidence, at=stamp)
            database.execute(
                "UPDATE token_discoveries SET status=?,checked_at=?,result_json=?,receipt_json=?,"
                "fingerprint=?,rejection_index=?,admitted_at=CASE WHEN ?='tracked' "
                "THEN COALESCE(admitted_at,?) ELSE admitted_at END,reward_eligible=CASE "
                "WHEN ?='tracked' AND claimed_at IS NULL THEN 0 ELSE reward_eligible END "
                "WHERE mint=?",
                (
                    status,
                    stamp,
                    canonical(result),
                    canonical(row.get("discovery") or {}),
                    mark,
                    rejection,
                    status,
                    stamp,
                    status,
                    mint,
                ),
            )
            if (
                passed
                and saved["submitter_id"]
                and saved["claimed_at"]
                and not saved["rewarded_at"]
                and saved["reward_eligible"]
            ):
                credit_flash(
                    database,
                    saved["submitter_id"],
                    saved["reward_amount"],
                    kind="memecoin_discovery",
                    reference_id="solana:" + mint,
                    at=at,
                )
                database.execute(
                    "UPDATE token_discoveries SET rewarded_at=? WHERE mint=?", (stamp, mint)
                )


def tracked_receipts(*, limit: int = 20, at: datetime | None = None) -> list[dict[str, Any]]:
    with connection() as database:
        _lock(database)
        rows = database.execute(
            "SELECT mint,receipt_json FROM token_discoveries "
            "WHERE status IN ('tracked','review') AND admitted_at IS NOT NULL "
            "AND receipt_json IS NOT NULL "
            "ORDER BY COALESCE(watch_read_at,first_seen_at),mint LIMIT ?",
            (limit,),
        ).fetchall()
        database.executemany(
            "UPDATE token_discoveries SET watch_read_at=? WHERE mint=?",
            [((at or datetime.now(UTC)).isoformat(), row["mint"]) for row in rows],
        )
    return [
        receipt for row in rows if (receipt := json.loads(row["receipt_json"])).get("pool_address")
    ]
