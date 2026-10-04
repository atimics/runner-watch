"""Leased wallet history pages with a saved buying-cost book."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from runner_web.db import connection
from runner_web.helius_discovery import _address
from runner_web.memecoin_evidence import CreditBudgetReached, budget_status
from runner_web.onchain_wallets import PAGE_SIZE, REFRESH_SECONDS, saved_wallet

LOG = logging.getLogger(__name__)
BACKFILL_SECONDS = 60
LEASE_SECONDS = 300
BACKFILL_PAGES = 5
PARALLEL_WALLETS = 2
BATCH_SECONDS = 120
BUDGET_WAIT_MESSAGE = "Wallet history will continue after the daily credit budget resets."


def advance_wallet(
    address: str,
    *,
    at: datetime | None,
    rpc: Callable[..., Any],
    download: Callable[..., bytes],
    collect: Callable[..., dict[str, Any]],
    max_pages: int = 1,
) -> dict[str, Any]:
    if type(max_pages) is not int or not 1 <= max_pages <= BACKFILL_PAGES:
        raise ValueError("Wallet batches contain one to five pages")
    address = _address(address)
    current = at or datetime.now(UTC)
    stamp = current.isoformat()
    token = secrets.token_hex(16)
    lease = (current + timedelta(seconds=LEASE_SECONDS)).isoformat()
    with connection() as database:
        claimed = database.execute(
            "INSERT INTO onchain_wallet_backfills"
            "(address,state_json,next_read_at,lease_until,lease_token,updated_at) "
            "VALUES(?,'{}',?,?,?,?) ON CONFLICT(address) DO UPDATE SET "
            "lease_until=excluded.lease_until,lease_token=excluded.lease_token "
            "WHERE onchain_wallet_backfills.lease_until<=? "
            "AND onchain_wallet_backfills.next_read_at<=? "
            "RETURNING state_json",
            (address, stamp, lease, token, stamp, stamp, stamp),
        ).fetchone()
    if claimed is None:
        return saved_wallet(address, at=current)
    state: dict[str, Any] = {}
    read_cache: dict[str, Any] = {}
    started = monotonic()
    for page in range(max_pages):
        next_read = current + timedelta(seconds=BACKFILL_SECONDS)
        try:
            if page == 0:
                state = json.loads(claimed["state_json"])
            extra = {"read_cache": read_cache} if max_pages > 1 else {}
            payload = collect(address, at=current, rpc=rpc, download=download, state=state, **extra)
            state = payload.pop("_state")
            more = payload["has_more"]
            keep_lease = more and page + 1 < max_pages and monotonic() - started < BATCH_SECONDS
            next_read = current + timedelta(seconds=BACKFILL_SECONDS if more else REFRESH_SECONDS)
        except Exception as error:
            # Provider exception text can contain the key; only fixed messages reach the page.
            message = "Chain data is temporarily unavailable. Try again in a minute."
            if isinstance(error, CreditBudgetReached):
                next_read = (current + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                message = BUDGET_WAIT_MESSAGE
            with connection() as database:
                owned = database.execute(
                    "UPDATE onchain_wallet_backfills SET lease_until=?,lease_token=NULL,"
                    "next_read_at=?,error=?,updated_at=? "
                    "WHERE address=? AND lease_token=? RETURNING address",
                    (stamp, next_read.isoformat(), message, stamp, address, token),
                ).fetchone()
                if owned is not None:
                    database.execute(
                        "INSERT INTO onchain_wallet_snapshots"
                        "(address,error,refresh_after,updated_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(address) DO UPDATE SET "
                        "error=excluded.error,refresh_after=excluded.refresh_after",
                        (address, message, next_read.isoformat(), stamp),
                    )
            LOG.info("Wallet history read will retry (%s)", type(error).__name__)
            break
        # Every page commits its cursor and snapshot before the next paid read.
        with connection() as database:
            owned = database.execute(
                "UPDATE onchain_wallet_backfills SET state_json=?,lease_until=?,lease_token=?,"
                "next_read_at=?,error=NULL,updated_at=? "
                "WHERE address=? AND lease_token=? RETURNING address",
                (
                    json.dumps(state, allow_nan=False),
                    lease if keep_lease else stamp,
                    token if keep_lease else None,
                    next_read.isoformat(),
                    stamp,
                    address,
                    token,
                ),
            ).fetchone()
            if owned is not None:
                database.execute(
                    "INSERT INTO onchain_wallet_snapshots"
                    "(address,payload_json,error,refresh_after,updated_at) VALUES(?,?,NULL,?,?) "
                    "ON CONFLICT(address) DO UPDATE SET "
                    "payload_json=excluded.payload_json,error=NULL,"
                    "refresh_after=excluded.refresh_after,updated_at=excluded.updated_at",
                    (address, json.dumps(payload, allow_nan=False), next_read.isoformat(), stamp),
                )
        if owned is None or not keep_lease:
            break
    return saved_wallet(address, at=current)


def resume_budget_waits(*, at: datetime) -> None:
    """Resume paused history when the allowance can cover a full wallet read."""
    budget = budget_status(at=at)
    read_credits = ((PAGE_SIZE + 99) // 100) * 10 + 3
    available = min(
        budget["remaining_credits"] - budget["price_reserve"],
        budget["wallet_daily_limit"] - budget["wallet_credits"],
    )
    if available < read_credits:
        return
    stamp = at.isoformat()
    with connection() as database:
        database.execute(
            "UPDATE onchain_wallet_backfills SET next_read_at=? "
            "WHERE error=? AND next_read_at>? AND lease_until<=?",
            (stamp, BUDGET_WAIT_MESSAGE, stamp, stamp),
        )


def _due_wallets(*, at: datetime, limit: int) -> list[str]:
    if not os.getenv("HELIUS_API_KEY", "").strip():
        return []
    resume_budget_waits(at=at)
    with connection() as database:
        rows = database.execute(
            "SELECT snapshots.address FROM onchain_wallet_snapshots snapshots "
            "LEFT JOIN onchain_wallet_backfills jobs ON jobs.address=snapshots.address "
            "WHERE jobs.address IS NULL OR (jobs.next_read_at<=? AND jobs.lease_until<=?) "
            "ORDER BY COALESCE(jobs.next_read_at,snapshots.updated_at),snapshots.address LIMIT ?",
            (at.isoformat(), at.isoformat(), limit),
        ).fetchall()
    return [row["address"] for row in rows]


def refresh_opened_wallet(*, at: datetime | None = None) -> dict[str, Any] | None:
    """Read one page for the oldest due wallet with a saved read."""
    from runner_web.onchain_wallets import refresh_wallet

    current = at or datetime.now(UTC)
    addresses = _due_wallets(at=current, limit=1)
    return refresh_wallet(addresses[0], at=current) if addresses else None


def refresh_opened_wallets(*, at: datetime | None = None) -> list[dict[str, Any]]:
    """Advance two due wallets, sharing balance and price reads within each batch."""
    from runner_web.onchain_wallets import refresh_wallet

    current = at or datetime.now(UTC)
    addresses = _due_wallets(at=current, limit=PARALLEL_WALLETS)
    if not addresses:
        return []
    with ThreadPoolExecutor(max_workers=PARALLEL_WALLETS) as pool:
        reads = [
            pool.submit(refresh_wallet, address, at=current, max_pages=BACKFILL_PAGES)
            for address in addresses
        ]
        return [read.result() for read in reads]


async def wallet_backfill_worker() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.to_thread(refresh_opened_wallets)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Wallet history worker will retry")
        await asyncio.sleep(5)
