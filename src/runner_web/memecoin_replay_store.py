"""Saved replay revisions and restart-safe rendering from existing receipts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from runner_web.db import connection
from runner_web.helius_discovery import _address
from runner_web.memecoin_replay import build_replay, canonical, verify_replay
from runner_web.memecoin_replay_gif import render_gif
from runner_web.telegram import config_from_env, memecoin_alerts_enabled

MAX_REVISIONS_PER_COIN = 16


def queue_replay(database, coin: dict, collected: str) -> None:
    if coin.get("network") != "solana":
        return
    try:
        _address(coin.get("token_address"))
    except (TypeError, ValueError):
        return
    database.execute(
        "INSERT INTO memecoin_replay_cases(coin_id,requested_at) VALUES(?,?) "
        "ON CONFLICT(coin_id) DO UPDATE SET requested_at=excluded.requested_at "
        "WHERE memecoin_replay_cases.requested_at<excluded.requested_at",
        (coin["id"], collected),
    )
    config = config_from_env()
    announce = memecoin_alerts_enabled() and config.configured
    database.execute(
        "INSERT INTO memecoin_replay_posts(coin_id,chat_id,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(coin_id) DO NOTHING",
        (
            coin["id"],
            config.chat_id if announce else "",
            "pending" if announce else "baseline",
            collected,
            collected,
        ),
    )


def _load_evidence(mint: str) -> tuple[list[dict], dict]:
    with connection() as database:
        count = database.execute(
            "SELECT COUNT(*) FROM memecoin_chain_events WHERE token_address=?",
            (mint,),
        ).fetchone()[0]
        seed = []
        for direction in ("ASC", "DESC"):
            seed.extend(
                database.execute(
                    "SELECT event_id,wallet,signature FROM memecoin_chain_events "
                    f"WHERE token_address=? ORDER BY observed_at {direction},event_id LIMIT 64",
                    (mint,),
                ).fetchall()
            )
        wallets = sorted({row["wallet"] for row in seed if row["wallet"]})[:16]
        related = []
        if wallets:
            placeholders = ",".join("?" for _ in wallets)
            related = database.execute(
                "SELECT signature FROM memecoin_chain_events "
                f"WHERE wallet IN ({placeholders}) "
                "AND (token_address<>? OR kind='sol_transfer') "
                "ORDER BY observed_at DESC,event_id LIMIT 64",
                (*wallets, mint),
            ).fetchall()
        signatures = sorted({row["signature"] for row in seed + related})
        receipts = []
        for signature in signatures:
            row = database.execute(
                "SELECT evidence_json,evidence_sha256,collected_at "
                "FROM memecoin_chain_transactions WHERE signature=?",
                (signature,),
            ).fetchone()
            if row:
                receipts.append(
                    {
                        "signature": signature,
                        "transaction": json.loads(row["evidence_json"]),
                        "sha256": row["evidence_sha256"],
                        "collected_at": row["collected_at"],
                    }
                )
        gaps = database.execute(
            "SELECT stream,start_time,end_time,reason FROM memecoin_chain_gaps "
            "WHERE stream LIKE ? ORDER BY start_time DESC LIMIT 20",
            ("program:%",),
        ).fetchall()
    return receipts, {
        "available_token_events": count,
        "wallet_context_limit": 16,
        "recent_program_gaps": [dict(row) for row in gaps],
        "scope": "First and latest token events plus bounded wallet context",
    }


def saved_replay(
    coin_id: str, replay_id: str | None = None, *, with_gif: bool = False
) -> dict | None:
    with connection() as database:
        row = database.execute(
            "SELECT r.id,r.payload_json,r.gif_sha256,r.created_at"
            + (",r.gif_bytes" if with_gif else "")
            + " FROM memecoin_replays r "
            + (
                "WHERE r.coin_id=? AND r.id=?"
                if replay_id
                else "JOIN memecoin_replay_cases c ON c.latest_id=r.id WHERE r.coin_id=?"
            ),
            (coin_id, replay_id) if replay_id else (coin_id,),
        ).fetchone()
    if not row:
        return None
    payload = json.loads(row["payload_json"])
    if payload["coin_id"] != coin_id or payload["id"] != row["id"] or not verify_replay(payload):
        raise ValueError("Stored replay quality checks failed")
    result = {"payload": payload, "gif_sha256": row["gif_sha256"], "created_at": row["created_at"]}
    if with_gif:
        gif = bytes(row["gif_bytes"])
        if hashlib.sha256(gif).hexdigest() != row["gif_sha256"]:
            raise ValueError("Stored GIF hash check failed")
        result["gif"] = gif
    return result


def replay_status(coin_id: str, replay_id: str | None = None) -> dict:
    record = saved_replay(coin_id, replay_id)
    with connection() as database:
        row = database.execute(
            "SELECT processed_at,last_error FROM memecoin_replay_cases WHERE coin_id=?",
            (coin_id,),
        ).fetchone()
    if record:
        payload = record["payload"]
        base = f"/api/memecoins/{coin_id}/replays/{payload['id']}"
        return {
            "status": "ready",
            "id": payload["id"],
            "created_at": record["created_at"],
            "collection_status": row["last_error"] if row else None,
            "payload": {key: value for key, value in payload.items() if key != "receipts"},
            "gif_url": base + ".gif",
            "evidence_url": base + ".json",
            "receipt_base": base + "/receipts/",
        }
    if row and row["last_error"] not in (None, "evidence_pending", "subject_scope"):
        return {"status": "retry", "message": "Replay checks need another collection pass."}
    return {
        "status": "pending",
        "message": "The replay will appear as chain evidence is collected.",
    }


def render_pending_replays(
    *, at: datetime | None = None, limit: int = 2, renderer=render_gif
) -> dict:
    current = at or datetime.now(UTC)
    stamp = current.isoformat()
    with connection() as database:
        pending = database.execute(
            "SELECT coin_id FROM memecoin_replay_cases "
            "WHERE (processed_at IS NULL OR requested_at>processed_at) "
            "AND (lease_until IS NULL OR lease_until<?) "
            "ORDER BY CASE WHEN latest_id IS NULL THEN 0 ELSE 1 END,requested_at,coin_id LIMIT ?",
            (stamp, max(1, min(limit, 10))),
        ).fetchall()
    result = {"ready": 0, "deferred": 0}
    for row in pending:
        token = str(uuid4())
        coin_id = row["coin_id"]
        with connection() as database:
            claimed = database.execute(
                "UPDATE memecoin_replay_cases SET lease_token=?,lease_until=? "
                "WHERE coin_id=? AND (lease_until IS NULL OR lease_until<?) "
                "AND (processed_at IS NULL OR requested_at>processed_at) RETURNING requested_at",
                (token, (current + timedelta(minutes=5)).isoformat(), coin_id, stamp),
            ).fetchone()
            asset = database.execute(
                "SELECT quote_json FROM memecoin_assets WHERE coin_id=?",
                (coin_id,),
            ).fetchone()
        if not claimed:
            continue
        try:
            coin = json.loads(asset["quote_json"])
            if coin.get("network") != "solana":
                raise ValueError("subject_scope")
            receipts, coverage = _load_evidence(coin["token_address"])
            previous = saved_replay(coin_id)
            if previous:
                receipts += previous["payload"]["receipts"]
            payload = build_replay(coin, receipts, coverage)
            same = previous and previous["payload"]["id"] == payload["id"]
            with connection() as database:
                count = database.execute(
                    "SELECT COUNT(*) FROM memecoin_replays WHERE coin_id=?",
                    (coin_id,),
                ).fetchone()[0]
            if not same and count >= MAX_REVISIONS_PER_COIN:
                raise ValueError("archive_capacity")
            gif = None if same else renderer(payload)
            if not verify_replay(payload):
                raise ValueError("quality_failed")
            with connection() as database:
                owned = database.execute(
                    "UPDATE memecoin_replay_cases SET latest_id=?,processed_at=?,lease_token=NULL,"
                    "lease_until=NULL,last_error=NULL WHERE coin_id=? AND lease_token=? "
                    "RETURNING coin_id",
                    (payload["id"], claimed["requested_at"], coin_id, token),
                ).fetchone()
                if not owned:
                    continue
                if not same:
                    database.execute(
                        "INSERT INTO memecoin_replays(id,coin_id,payload_json,gif_bytes,gif_sha256,"
                        "created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                        (
                            payload["id"],
                            coin_id,
                            canonical(payload),
                            gif,
                            hashlib.sha256(gif).hexdigest(),
                            stamp,
                        ),
                    )
                database.execute(
                    "UPDATE memecoin_replay_posts SET replay_id=? WHERE coin_id=? "
                    "AND status='pending' AND replay_id IS NULL",
                    (payload["id"], coin_id),
                )
            result["ready"] += 1
        except Exception as exc:
            code = (
                str(exc)
                if isinstance(exc, ValueError)
                and str(exc)
                in {
                    "evidence_pending",
                    "subject_scope",
                    "receipt_hash",
                    "decoder_replay",
                    "conflicting_receipt",
                    "quality_failed",
                    "archive_capacity",
                }
                else "render_failed"
            )
            with connection() as database:
                database.execute(
                    "UPDATE memecoin_replay_cases SET processed_at=CASE WHEN ?='render_failed' "
                    "THEN processed_at ELSE ? END,lease_token=NULL,"
                    "lease_until=?,last_error=? WHERE coin_id=? AND lease_token=?",
                    (
                        code,
                        claimed["requested_at"],
                        (current + timedelta(seconds=60)).isoformat()
                        if code == "render_failed"
                        else None,
                        code,
                        coin_id,
                        token,
                    ),
                )
            result["deferred"] += 1
    return result
