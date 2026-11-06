"""Small, explicit API projections over saved Solana evidence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from runner_web import api_credits, api_insights
from runner_web.db import connection
from runner_web.helius_discovery import _address, rpc_request
from runner_web.memecoin_store import stored_memecoin

LOG = logging.getLogger(__name__)


def address(value: str) -> str:
    try:
        return _address(value)
    except ValueError as exc:
        raise HTTPException(422, "Use a Solana address with 32 bytes.") from exc


def snapshot(coin_id: str) -> dict[str, Any]:
    saved = stored_memecoin(coin_id)
    if not saved:
        raise HTTPException(404, "A saved token snapshot is needed for this read.")
    return _snapshot(coin_id, saved)


def _snapshot(coin_id: str, saved: dict[str, Any]) -> dict[str, Any]:
    row = saved["coin"]
    early = row.get("early") or {}
    return {
        "id": coin_id,
        "name": row.get("name"),
        "symbol": row.get("symbol"),
        "mint": row.get("token_address"),
        "score": early.get("score"),
        "state": early.get("state"),
        "observed_at": row.get("observed_at"),
        "collected_at": saved["collected_at"],
        "source": "Runner Watch",
        "freshness": api_insights.freshness(row.get("observed_at"), at=datetime.now(UTC)),
    }


def tokens(after: str, limit: int) -> dict[str, Any]:
    # The worker already owns a bounded market snapshot. A page uses one saved revision.
    with connection() as db:
        saved = db.execute(
            "SELECT value FROM worker_state WHERE key='memecoins_snapshot'"
        ).fetchone()
    data = json.loads(saved["value"]) if saved else {}
    revision = api_credits.digest(str(data.get("collected_at")))[:12]
    offset = 0
    if after:
        try:
            version, index = after.split(":")
            offset = int(index)
            if version != revision or not 0 <= offset <= 10000:
                raise ValueError("Snapshot changed")
        except ValueError as exc:
            raise HTTPException(409, "Start a new list page for the current snapshot.") from exc
    rows = sorted(
        data.get("rows", []),
        key=lambda item: (float((item.get("early") or {}).get("score") or 0), str(item.get("id"))),
        reverse=True,
    )
    items = []
    for row in rows[offset : offset + limit]:
        early = row.get("early") or {}
        items.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "symbol": row.get("symbol"),
                "mint": row.get("token_address"),
                "score": early.get("score"),
                "state": early.get("state"),
                "observed_at": row.get("observed_at"),
            }
        )
    return {
        "items": items,
        "collected_at": data.get("collected_at"),
        "next_cursor": f"{revision}:{offset + limit}" if len(rows) > offset + limit else None,
        "order": "score descending",
    }


def analysis(coin_id: str) -> dict[str, Any]:
    from runner_web.memecoin_lookup import lookup_memecoin

    saved = stored_memecoin(coin_id)
    if not saved:
        raise HTTPException(404, "A saved token snapshot is needed for this read.")
    row = saved["coin"]
    mint = row.get("token_address")
    basics = lookup_memecoin(mint, fetch=False) if mint else None
    if basics:
        basics = {
            key: basics.get(key)
            for key in ("address", "status", "checked_at", "controls", "slot", "facts", "metadata")
        }
    with connection() as db:
        previous = db.execute(
            "SELECT observed_at,features_json FROM memecoin_quote_history "
            "WHERE coin_id=? AND observed_at<? AND features_json IS NOT NULL "
            "ORDER BY observed_at DESC LIMIT 1",
            (coin_id, row.get("observed_at") or ""),
        ).fetchone()
        saved_findings = db.execute(
            "SELECT value FROM worker_state WHERE key='memecoin_forensics'"
        ).fetchone()
    findings = json.loads(saved_findings["value"]).get("findings", []) if saved_findings else []
    return {
        **_snapshot(coin_id, saved),
        "controls": basics,
        "findings": [item for item in findings if item.get("token_address") == mint][:100],
        "ratification": row.get("ratification") or row.get("ratify"),
        "evidence_url": "/api/memecoins/" + coin_id + "/replay",
        "insights": api_insights.token_insights(
            row,
            basics,
            {
                "observed_at": previous["observed_at"],
                "features": json.loads(previous["features_json"]),
            }
            if previous
            else None,
        ),
    }


def analysis_by_mint(value: str) -> dict[str, Any]:
    mint = address(value)
    coin_id = "chain-" + api_credits.digest("solana:" + mint)
    result = analysis(coin_id)
    if result["mint"] != mint:
        raise HTTPException(404, "A saved report for this mint is pending.")
    return result


def wallet(value: str, cursor: str = "") -> dict[str, Any]:
    from runner_web.onchain_wallets import HOLDINGS_PER_PAGE, holdings_page, saved_wallet

    data = saved_wallet(address(value))
    if data["status"] == "pending":
        raise HTTPException(404, "A saved wallet report is pending. Fresh scans use the jobs API.")
    data.pop("error", None)
    data.pop("provider_ready", None)
    revision = api_credits.digest(
        json.dumps([value, data.get("updated_at"), data.get("history_hash")])
    )[:16]
    page = 1
    if cursor:
        try:
            version, position = cursor.split(":")
            page = int(position)
            if version != revision or not 1 <= page <= 200:
                raise ValueError("Wallet snapshot changed")
            if (page - 1) * HOLDINGS_PER_PAGE >= len(data.get("holdings") or []):
                raise ValueError("Page exceeds holdings")
        except ValueError as exc:
            raise HTTPException(409, "Start a new wallet page for the current snapshot.") from exc
    result = holdings_page(data, page)
    result["insights"] = api_insights.wallet_insights(data)
    result["next_cursor"] = f"{revision}:{page + 1}" if page < result["holdings_pages"] else None
    return result


def history(coin_id: str, before: str, limit: int) -> dict[str, Any]:
    snapshot(coin_id)
    with connection() as db:
        rows = db.execute(
            "SELECT observed_at,features_json FROM memecoin_quote_history "
            "WHERE coin_id=? AND observed_at<? ORDER BY observed_at DESC LIMIT ?",
            (coin_id, before or "9999", limit + 1),
        ).fetchall()
    return {
        "items": [
            {
                "observed_at": row["observed_at"],
                "analysis": json.loads(row["features_json"] or "{}"),
            }
            for row in rows[:limit]
        ],
        "next_cursor": rows[limit - 1]["observed_at"] if len(rows) > limit else None,
        "coverage": "Saved observations within the current retention window",
    }


def require_capacity(database: Any, operation: str) -> None:
    from runner_web.memecoin_evidence import lane_limit

    current = datetime.now(UTC)
    day = current.date().isoformat()
    database.execute("UPDATE api_job_guard SET id=id WHERE id=1")
    queued = database.execute(
        "SELECT COUNT(*) AS n,COALESCE(SUM(CASE WHEN operation='wallet_scan' "
        "THEN 103 ELSE 1 END),0) AS credits "
        "FROM api_requests WHERE status IN ('queued','running')"
    ).fetchone()
    if queued["n"] >= 20:
        raise HTTPException(
            429, "The scan queue is busy. Retry shortly.", headers={"Retry-After": "60"}
        )
    api_used = database.execute(
        "SELECT credits FROM api_provider_budget WHERE day=?", (day,)
    ).fetchone()
    shared_used = database.execute(
        "SELECT reserved_credits FROM memecoin_helius_budget WHERE day=?", (day,)
    ).fetchone()
    api_limit = max(0, min(5000, int(os.getenv("API_HELIUS_DAILY_CREDITS", "2000"))))
    needed = (103 if operation == "wallet_scan" else 1) + queued["credits"]
    if needed > api_limit - (api_used["credits"] if api_used else 0) or needed > lane_limit(
        "other"
    ) - (shared_used["reserved_credits"] if shared_used else 0):
        raise HTTPException(
            429, "The scan budget is busy. Saved data is available.", headers={"Retry-After": "60"}
        )


def provider_rpc(body: dict[str, Any], *, credits: int, **kwargs: Any) -> dict[str, Any]:
    # This allowance sits inside the existing shared provider budget and price reserve.
    limit = max(0, min(5000, int(os.getenv("API_HELIUS_DAILY_CREDITS", "2000"))))
    day = datetime.now(UTC).date().isoformat()
    if credits > limit:
        raise RuntimeError("API provider budget reached")
    with connection() as db:
        row = db.execute(
            "INSERT INTO api_provider_budget(day,credits) VALUES(?,?) "
            "ON CONFLICT(day) DO UPDATE SET credits=api_provider_budget.credits+excluded.credits "
            "WHERE api_provider_budget.credits+excluded.credits<=? RETURNING day",
            (day, credits, limit),
        ).fetchone()
    if row is None:
        raise RuntimeError("API provider budget reached")
    return rpc_request(body, credits=credits, lane="other", **kwargs)


def token_check(mint: str) -> dict[str, Any]:
    from runner_web.helius_discovery import _decode
    from runner_web.memecoin_lookup import METADATA_PROGRAM, _chain_basics
    from runner_web.solana_keys import find_program_address

    meta = find_program_address(
        [b"metadata", _decode(METADATA_PROGRAM, 44), _decode(mint, 44)], METADATA_PROGRAM
    )
    reply = provider_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getMultipleAccounts",
            "params": [[mint, meta], {"encoding": "jsonParsed", "commitment": "finalized"}],
        },
        credits=1,
        timeout=10,
    )
    value = _chain_basics(mint, reply)
    if value.get("status") != "ready":
        raise ValueError("A valid token mint is required")
    return {**value, "address": mint, "checked_at": api_credits.stamp()}


def run_job(job: dict[str, Any]) -> None:
    try:
        parameters = json.loads(job["parameters"])
        if job["operation"] == "token_check":
            result = token_check(parameters["address"])
        else:
            from runner_web.onchain_wallets import collect_wallet

            state = None
            if parameters.get("cursor"):
                previous = api_credits.receipt(parameters["cursor"], job["user_id"])
                if previous["status"] != "complete" or previous["operation"] != "wallet_scan":
                    raise ValueError("Use a completed wallet scan cursor")
                if json.loads(previous["parameters"])["address"] != parameters["address"]:
                    raise ValueError("Use a cursor for this wallet")
                state = json.loads(previous["result_json"])["_state"]
            # Quote-currency PnL uses chain receipts. USD marks belong to the saved app view.
            result = collect_wallet(
                parameters["address"],
                at=datetime.now(UTC),
                state=state,
                rpc=provider_rpc,
                download=lambda *_: b"{}",
            )
            result["next_cursor"] = job["id"] if result.get("has_more") else None
            result["insights"] = api_insights.wallet_insights(result)
        if len(json.dumps(result).encode()) > 524288:
            raise ValueError("Wallet page exceeds the result limit")
        api_credits.finish(job["id"], result=result)
    except Exception:
        LOG.warning("API job failed operation=%s", job["operation"])
        api_credits.finish(
            job["id"], error="The provider could not complete this page. Credits returned."
        )


def run_pending() -> None:
    api_credits.prune()
    if os.getenv("API_FRESH_JOBS_ENABLED", "0") != "1":
        return
    with connection() as db:
        db.execute("UPDATE api_job_guard SET id=id WHERE id=1")
        running = db.execute(
            "SELECT COUNT(*) AS n FROM api_requests WHERE status='running'"
        ).fetchone()
        if running["n"] >= 2:
            return
        row = db.execute(
            "SELECT id FROM api_requests WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return
        claimed = db.execute(
            "UPDATE api_requests SET status='running',updated_at=? "
            "WHERE id=? AND status='queued' RETURNING *",
            (api_credits.stamp(), row["id"]),
        ).fetchone()
    if claimed:
        run_job(dict(claimed))


async def worker() -> None:
    while True:
        try:
            await asyncio.to_thread(run_pending)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("API queue will retry")
        await asyncio.sleep(5)
