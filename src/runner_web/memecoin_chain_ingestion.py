"""Resumable Solana ingestion with bounded work and a shared Helius budget."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from runner_web import memecoin_evidence as evidence
from runner_web.helius_discovery import Rpc, rpc_request
from runner_web.memecoin_chain_parser import PROGRAMS, SOL, parse_events, valid_transactions

PAGE_SIZE = 100
# Pump's migration authority (Global.withdraw_authority, read from the Global
# account 2026-09-26). Every graduation to PumpSwap involves it, so its history
# is a stream of graduations: about a thousand a day, where sampling the
# programs caught about eleven.
MIGRATION_AUTHORITY = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
# Pump's mint authority (the PDA of "mint-authority"): every launch and nothing
# else, about 38,000 a day, where the program samples caught about 680.
MINT_AUTHORITY = "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM"
# The launch stream reads the newest launches only: about 100 land in 4 minutes.
LAUNCH_WINDOW_SECONDS = 240
# Paid reads at most this often. The graduation page is read every time (about
# ten graduations land in 15 minutes); the other streams take turns, launches
# every other turn because they feed the bonding-curve list.
PAID_READ_SECONDS = 900
SAMPLE_ROTATION = (
    ("launches", MINT_AUTHORITY),
    ("program:pumpswap", PROGRAMS["pumpswap"]),
    ("launches", MINT_AUTHORITY),
    ("program:pump", PROGRAMS["pump"]),
    ("launches", MINT_AUTHORITY),
    ("program:raydium_cpmm", PROGRAMS["raydium_cpmm"]),
)


def ingest_stream(stream: str, address: str, *, at: datetime, rpc: Rpc) -> dict[str, Any]:
    state = evidence.stream_state(stream)
    if stream == "launches":
        # A sample of the newest, not a backlog to catch up on.
        newest = int(at.timestamp()) - 60
        state = {"window_start": newest - LAUNCH_WINDOW_SECONDS, "window_end": newest}
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
        "maxSupportedTransactionVersion": 1,
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
    if len(entries) < PAGE_SIZE:
        # Helius can return a next-page token after a partial page; the follow-up
        # read came back empty and still cost 10 credits, so a partial page ends
        # the window.
        cursor = None
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


def collect_chain(*, at: datetime, rpc: Rpc | None = None, quiet: bool = False) -> dict[str, Any]:
    """Paid reads when due, then the saved events they add to.

    Paid reads run at most once every 15 minutes: the graduation page, one
    sample from the rotation and one wallet page, at most 30 credits. Quiet
    (nobody reading memecoin pages) keeps only the graduation page. A spent
    budget skips the reads and returns the saved events, so prices go on.
    """

    call = rpc or rpc_request
    schedule = evidence.stream_state("schedule")
    offset = schedule.get("offset", 0)
    last_paid = schedule.get("paid_at")
    paid = last_paid is None or not 0 <= at.timestamp() - last_paid < PAID_READ_SECONDS
    transactions = []
    errors = []
    successes = 0
    pages = [("graduations", MIGRATION_AUTHORITY)] if paid else []
    if paid and not quiet:
        pages.append(SAMPLE_ROTATION[offset % len(SAMPLE_ROTATION)])
    for stream, address in pages:
        name = stream.split(":", 1)[-1]
        try:
            batch = ingest_stream(stream, address, at=at, rpc=call)
            transactions.extend(batch["transactions"])
            successes += 1
        except evidence.CreditBudgetReached:
            errors.append("Daily credit budget reached")
            break
        except (ValueError, TypeError, KeyError):
            errors.append("Program page failed: " + name)
    spent = "Daily credit budget reached" in errors
    if successes == 0 and errors and not spent:
        raise ValueError("Helius ingestion deferred: " + "; ".join(errors))
    if paid:
        evidence.save_batch(
            "schedule",
            [],
            [],
            {
                "offset": (offset + (0 if quiet else 1)) % len(SAMPLE_ROTATION),
                "paid_at": int(at.timestamp()),
            },
            at=at,
        )
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
    if wallets and paid and not quiet and not spent:
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
    # A launch trades on its bonding curve until it graduates to a pool.
    graduated = {pool["token_address"] for pool in pools}
    curves = [
        {
            "pool_address": event["bonding_curve"],
            "token_address": event["token_address"],
            "quote_address": SOL,
            "pool_creator": event["wallet"],
            "declared_creator": event.get("declared_creator"),
            "network": "solana",
            "created_at": event["observed_at"],
            "signature": event["signature"],
            "slot": event["slot"],
            "program": event["program"],
            "source_url": event["source_url"],
            "commitment": "finalized",
            "venue": "bonding_curve",
        }
        for event in events
        if event["kind"] == "token_launch"
        and event.get("bonding_curve")
        and event["token_address"] not in graduated
    ]
    return {
        "pools": pools,
        "curves": curves,
        "transactions": transactions,
        "events": events,
        "received_transactions": len(transactions),
        "partial": True,
        "errors": errors,
        "coverage": evidence.evidence_status(at=at),
    }
