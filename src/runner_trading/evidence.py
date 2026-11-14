"""Bounded, receipt-backed swap samples for a pending SETUP decision.

This uses the shared non-price Helius budget. A successful empty page is a
checked sample, not proof of no market activity. Failed reads never freshen it.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from runner_trading.config import Policy, age, stamp
from runner_trading.strategy import non_swap_entry_reasons
from runner_web import memecoin_evidence as store
from runner_web.helius_discovery import Rpc, _address, rpc_request
from runner_web.memecoin_chain_parser import parse_events, valid_transactions
from runner_web.memecoin_forensics import analyze_events
from runner_web.memecoin_model import assess_memecoin

MAX_PAGES = 2
PAGE_SIZE = 100
CACHE_SECONDS = 60
WINDOW_SECONDS = 900
SAMPLE_MAX_AGE_SECONDS = 300


def _stream(row: dict) -> str:
    return "trading:swaps:" + _address(row["token_address"]) + ":" + _address(row["pool_address"])


def _recent_swaps(events: list[dict], row: dict, at: datetime) -> list[dict]:
    # Several instructions can describe the same transaction's net token change.
    # One signature must not satisfy the three-transaction entry threshold.
    distinct = {}
    for event in events:
        if (
            event.get("kind") == "swap"
            and event.get("token_address") == row.get("token_address")
            and event.get("pool_address") == row.get("pool_address")
            and event.get("commitment") == "finalized"
            and event.get("direction") in {"buy", "sell"}
            and event.get("wallet")
            and event.get("net_token_amount")
            and event.get("signature")
            and 0 <= age(event.get("observed_at"), at) <= WINDOW_SECONDS
        ):
            distinct.setdefault(event["signature"], event)
    return list(distinct.values())


def _apply(row: dict, state: dict, at: datetime) -> dict:
    result = deepcopy(row)
    result.pop("_trading_evidence_deferred", None)
    assessment = result.setdefault("memecoin_assessment", {})
    coverage = assessment.setdefault("coverage", {})
    sample = state.get("sample") or {}
    matching = (
        sample.get("mint") == row.get("token_address")
        and sample.get("pool") == row.get("pool_address")
        and 0 <= age(sample.get("checked_at"), at) <= SAMPLE_MAX_AGE_SECONDS
    )
    coverage.update(
        swap_attempted_at=state.get("attempted_at"),
        swap_attempt_state=state.get("attempt_state", "unchecked"),
        swap_source="finalized_pool_transactions",
        swap_token_address=row.get("token_address"),
        swap_pool_address=row.get("pool_address"),
        swap_checked_at=sample.get("checked_at") if matching else None,
        swap_window_start=sample.get("window_start") if matching else None,
        swap_window_end=sample.get("window_end") if matching else None,
        swap_partial=sample.get("partial", True),
        swap_state=state.get("attempt_state", "unchecked"),
    )
    if not matching:
        # Do not present the board's incidental program sample as this pool's read.
        assessment["sentiment"] = {
            **(assessment.get("sentiment") or {}),
            "observed_swaps": 0,
        }
        return result
    events = _recent_swaps(sample.get("events") or [], row, at)
    previous_risk = (assessment.get("risk") or {}).get("factors") or []
    findings = previous_risk + analyze_events(events)["findings"]
    updated = assess_memecoin(
        row,
        events=events,
        findings=findings,
        coverage={"checked_at": coverage.get("chain_checked_at"), "partial": True},
        at=at,
    )["memecoin_assessment"]
    assessment["sentiment"] = updated["sentiment"]
    # New sampled risk can block an entry; collection cannot erase a board finding.
    factors = {finding["id"]: finding for finding in updated["risk"]["factors"]}
    for index, finding in enumerate(previous_risk):
        factors.setdefault(finding.get("id", "saved:" + str(index)), finding)
    assessment["risk"] = {
        **(assessment.get("risk") or {}),
        "factors": list(factors.values()),
        "state": "detected" if factors else (assessment.get("risk") or {}).get("state", "unknown"),
    }
    coverage["swap_state"] = "sampled" if events else "empty"
    return result


def apply_cached_evidence(row: dict, *, at: datetime) -> dict:
    """Reattach dated pool evidence before a queued buy checks a fresh board row."""
    if row.get("network") != "solana" or row.get("venue") != "pool":
        return row
    try:
        state = store.stream_state(_stream(row))
    except (KeyError, TypeError, ValueError):
        state = {}
    return _apply(row, state, at)


def _collect(row: dict, state: dict, *, at: datetime, rpc: Rpc) -> dict:
    stream = _stream(row)
    start, end = int((at - timedelta(seconds=WINDOW_SECONDS)).timestamp()), int(at.timestamp())
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransactionsForAddress",
        "params": [
            row["pool_address"],
            {
                "transactionDetails": "full",
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 1,
                "commitment": "finalized",
                "sortOrder": "desc",
                "limit": PAGE_SIZE,
                "filters": {"status": "succeeded", "blockTime": {"gte": start, "lt": end}},
            },
        ],
    }
    next_state = {**state, "attempted_at": stamp(at)}
    entries, events = [], []
    try:
        payload = rpc(request)
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError("Invalid transaction response")
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            raise ValueError("Invalid transaction result")
        raw = result.get("data")
        if not isinstance(raw, list) or len(raw) > PAGE_SIZE:
            raise ValueError("Invalid transaction page")
        entries = [
            entry for entry in valid_transactions(raw, at=at) if start <= entry["blockTime"] < end
        ]
        events = [event for entry in entries for event in parse_events(entry)]
        swaps = _recent_swaps(events, row, at)
        next_state.update(
            attempt_state="checked",
            sample={
                "mint": row["token_address"],
                "pool": row["pool_address"],
                "checked_at": stamp(at),
                "window_start": stamp(at - timedelta(seconds=WINDOW_SECONDS)),
                "window_end": stamp(at),
                "partial": bool(result.get("paginationToken"))
                or len(raw) == PAGE_SIZE
                or len(entries) != len(raw),
                "received_transactions": len(raw),
                "events": swaps,
            },
        )
    except store.CreditBudgetReached:
        next_state["attempt_state"] = "budget_exhausted"
    except (KeyError, TypeError, ValueError, IndexError):
        next_state["attempt_state"] = "failed"
    store.save_batch(stream, entries, events, next_state, at=at)
    return next_state


def enrich_candidates(
    rows: list[dict[str, Any]], *, at: datetime, policy: Policy, rpc: Rpc | None = None
) -> list[dict[str, Any]]:
    """Enrich pending signals fairly; defer bounded/failed work without consuming it."""
    call = rpc or partial(rpc_request, at=at, timeout=8)
    result = [deepcopy(row) for row in rows]
    pending = []
    for index, row in enumerate(result):
        if non_swap_entry_reasons(row, policy, at):
            continue
        try:
            state = store.stream_state(_stream(row))
        except (KeyError, TypeError, ValueError):
            continue
        pending.append((state.get("attempted_at") or "", row.get("observed_at", ""), index, state))
    pages = 0
    for _, _, index, state in sorted(pending, key=lambda item: (item[0], item[1], item[2])):
        row = result[index]
        # Another candidate can share this pool; use a sample saved earlier in this call.
        state = store.stream_state(_stream(row))
        if not 0 <= age(state.get("attempted_at"), at) < CACHE_SECONDS:
            if pages >= MAX_PAGES:
                result[index]["_trading_evidence_deferred"] = True
                continue
            pages += 1
            state = _collect(row, state, at=at, rpc=call)
        result[index] = _apply(row, state, at)
        coverage = result[index]["memecoin_assessment"]["coverage"]
        if coverage["swap_state"] not in {"sampled", "empty"}:
            result[index]["_trading_evidence_deferred"] = True
    return result
