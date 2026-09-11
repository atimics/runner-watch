"""Resumable Solana ingestion with bounded work and a shared Helius budget."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from runner_web import memecoin_evidence as evidence
from runner_web.helius_discovery import Rpc, rpc_request
from runner_web.memecoin_chain_parser import PROGRAMS, parse_events, valid_transactions

PAGE_SIZE = 100


def ingest_stream(stream: str, address: str, *, at: datetime, rpc: Rpc) -> dict[str, Any]:
    state = evidence.stream_state(stream)
    end = state.get("window_end") or int(at.timestamp()) - 60
    start = state.get(
        "window_start",
        state.get("completed_until", end - (86400 if stream.startswith("wallet:") else 300)),
    )
    gaps = list(state.get("coverage_gaps", []))
    current_end = int(at.timestamp()) - 60
    if stream.startswith("program:") and current_end - start > 3600:
        fresh_start = current_end - 300
        gaps.append({"start": start, "end": fresh_start, "reason": "credit_bounded_backlog"})
        state = {**state, "cursor": None}
        start, end = fresh_start, current_end
    options = {
        "transactionDetails": "full",
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
        "commitment": "finalized",
        "sortOrder": "asc",
        "limit": PAGE_SIZE,
        "filters": {"status": "succeeded", "blockTime": {"gte": start, "lt": end}},
    }
    if state.get("cursor"):
        options["paginationToken"] = state["cursor"]
    payload = rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransactionsForAddress",
            "params": [address, options],
        }
    )
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError("Helius RPC returned an error")
    result = payload.get("result", {})
    entries = result.get("data")
    cursor = result.get("paginationToken")
    if not isinstance(entries, list) or len(entries) > PAGE_SIZE:
        raise ValueError("Invalid Helius transaction page")
    if cursor is not None and (
        not isinstance(cursor, str) or len(cursor) > 128 or cursor == state.get("cursor")
    ):
        raise ValueError("Invalid Helius pagination progress")
    valid = [
        entry for entry in valid_transactions(entries, at=at) if start <= entry["blockTime"] < end
    ]
    events = [event for entry in valid for event in parse_events(entry)]
    completed = state.get("completed_until", start)
    next_state = {
        "address": address,
        "cursor": cursor,
        "window_start": start,
        "window_end": end,
        "completed_until": completed,
        "received_transactions": len(entries),
        "decoded_events": len(events),
        "excluded_transactions": len(entries) - len(valid),
        "backlog_seconds": max(0, int(at.timestamp()) - completed),
        "has_more": bool(cursor),
        "commitment": "finalized",
        "coverage_gaps": gaps[-20:],
    }
    if not cursor:
        next_state.update(
            completed_until=end,
            window_start=end,
            window_end=None,
            backlog_seconds=max(0, int(at.timestamp()) - end),
        )
    evidence.save_batch(stream, valid, events, next_state, at=at)
    return {"transactions": valid, "events": events, "state": next_state}


def collect_chain(*, at: datetime, rpc: Rpc | None = None) -> dict[str, Any]:
    call = rpc or rpc_request
    schedule = evidence.stream_state("schedule")
    offset = schedule.get("offset", 0)
    programs = list(PROGRAMS.items())
    transactions = []
    errors = []
    successes = 0
    # Two program pages and one wallet page cost at most 30 credits per run.
    for index in range(2):
        name, address = programs[(offset + index) % len(programs)]
        try:
            batch = ingest_stream("program:" + name, address, at=at, rpc=call)
            transactions.extend(batch["transactions"])
            successes += 1
        except evidence.CreditBudgetReached:
            errors.append("Daily credit budget reached")
            break
        except (ValueError, TypeError, KeyError):
            errors.append("Program page failed: " + name)
    if successes == 0 and errors:
        raise ValueError("Helius ingestion deferred: " + "; ".join(errors))
    evidence.save_batch("schedule", [], [], {"offset": (offset + 2) % len(programs)}, at=at)
    events = evidence.recent_events(at=at)
    context = evidence.recent_events(at=at, kind="pool_created", limit=1000)
    context += evidence.recent_events(at=at, kind="token_launch", limit=1000)
    events = list({event["event_id"]: event for event in events + context}.values())
    # Wallet pages add funding and exit evidence. Rotate across observed creators.
    wallets = {
        event["wallet"] for event in events if event["kind"] in ("pool_created", "token_launch")
    }
    wallets.update(
        event["declared_creator"]
        for event in events
        if event.get("declared_creator") and event["declared_creator"] != "1" * 32
    )
    buyers = {
        event["wallet"]
        for event in events
        if event["kind"] == "swap" and event.get("direction") == "buy"
    }
    if buyers and offset % 3 == 0:
        wallets = buyers
    if wallets:
        progress = {row["stream"]: row for row in evidence.evidence_status(at=at)["streams"]}
        wallet = min(
            wallets,
            key=lambda value: (
                progress.get("wallet:" + value, {}).get("completed_until", 0),
                value,
            ),
        )
        try:
            batch = ingest_stream("wallet:" + wallet, wallet, at=at, rpc=call)
            transactions.extend(batch["transactions"])
        except evidence.CreditBudgetReached:
            errors.append("Daily credit budget reached")
        except (ValueError, TypeError, KeyError):
            errors.append("Wallet page failed")
    evidence.prune_evidence(at=at)
    events = evidence.recent_events(at=at)
    context = evidence.recent_events(at=at, kind="pool_created", limit=1000)
    context += evidence.recent_events(at=at, kind="token_launch", limit=1000)
    events = list({event["event_id"]: event for event in events + context}.values())
    pools = []
    for event in events:
        if event["kind"] == "pool_created":
            pools.append(
                {
                    "pool_address": event["pool_address"],
                    "token_address": event["token_address"],
                    "quote_address": event["quote_address"],
                    "pool_creator": event["wallet"],
                    "declared_creator": event.get("declared_creator"),
                    "network": "solana",
                    "created_at": event["observed_at"],
                    "signature": event["signature"],
                    "slot": event["slot"],
                    "program": event["program"],
                    "source_url": event["source_url"],
                    "commitment": "finalized",
                }
            )
    return {
        "pools": pools,
        "transactions": transactions,
        "events": events,
        "received_transactions": len(transactions),
        "partial": True,
        "errors": errors,
        "coverage": evidence.evidence_status(at=at),
    }
