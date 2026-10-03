"""Leased wallet history pages with a saved buying-cost book."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.db import connection
from runner_web.helius_discovery import _address
from runner_web.memecoin_evidence import CreditBudgetReached
from runner_web.onchain_wallets import REFRESH_SECONDS, saved_wallet

LOG = logging.getLogger(__name__)
BACKFILL_SECONDS = 60
LEASE_SECONDS = 300


def advance_wallet(
    address: str,
    *,
    at: datetime | None,
    rpc: Callable[..., Any],
    download: Callable[..., bytes],
    collect: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
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
    next_read = current + timedelta(seconds=BACKFILL_SECONDS)
    try:
        state = json.loads(claimed["state_json"])
        payload = collect(address, at=current, rpc=rpc, download=download, state=state)
        state = payload.pop("_state")
        next_read = current + timedelta(
            seconds=BACKFILL_SECONDS if payload["has_more"] else REFRESH_SECONDS
        )
    except Exception as error:
        # Provider exception text can contain the key; only fixed messages reach the page.
        message = "Chain data is temporarily unavailable. Try again in a minute."
        if isinstance(error, CreditBudgetReached):
            next_read = (current + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            message = "Wallet history will continue after the daily credit budget resets."
        with connection() as database:
            owned = database.execute(
                "UPDATE onchain_wallet_backfills SET lease_until=?,lease_token=NULL,"
                "next_read_at=?,error=?,updated_at=? "
                "WHERE address=? AND lease_token=? RETURNING address",
                (stamp, next_read.isoformat(), message, stamp, address, token),
            ).fetchone()
            if owned is not None:
                database.execute(
                    "INSERT INTO onchain_wallet_snapshots(address,error,refresh_after,updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(address) DO UPDATE SET "
                    "error=excluded.error,refresh_after=excluded.refresh_after",
                    (address, message, next_read.isoformat(), stamp),
                )
        LOG.info("Wallet history read will retry (%s)", type(error).__name__)
        return saved_wallet(address, at=current)
    with connection() as database:
        owned = database.execute(
            "UPDATE onchain_wallet_backfills SET state_json=?,lease_until=?,lease_token=NULL,"
            "next_read_at=?,error=NULL,updated_at=? "
            "WHERE address=? AND lease_token=? RETURNING address",
            (
                json.dumps(state, allow_nan=False),
                stamp,
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
                "ON CONFLICT(address) DO UPDATE SET payload_json=excluded.payload_json,error=NULL,"
                "refresh_after=excluded.refresh_after,updated_at=excluded.updated_at",
                (address, json.dumps(payload, allow_nan=False), next_read.isoformat(), stamp),
            )
    return saved_wallet(address, at=current)


def refresh_opened_wallet(*, at: datetime | None = None) -> dict[str, Any] | None:
    """The oldest due wallet with a saved read gets one page per worker pass."""
    if not os.getenv("HELIUS_API_KEY", "").strip():
        return None
    from runner_web.onchain_wallets import refresh_wallet

    current = at or datetime.now(UTC)
    with connection() as database:
        row = database.execute(
            "SELECT snapshots.address FROM onchain_wallet_snapshots snapshots "
            "LEFT JOIN onchain_wallet_backfills jobs ON jobs.address=snapshots.address "
            "WHERE jobs.address IS NULL OR (jobs.next_read_at<=? AND jobs.lease_until<=?) "
            "ORDER BY COALESCE(jobs.next_read_at,snapshots.updated_at),snapshots.address LIMIT 1",
            (current.isoformat(), current.isoformat()),
        ).fetchone()
    return refresh_wallet(row["address"], at=current) if row else None


async def wallet_backfill_worker() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.to_thread(refresh_opened_wallet)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("Wallet history worker will retry")
        await asyncio.sleep(5)
