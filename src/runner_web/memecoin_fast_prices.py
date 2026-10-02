"""A fast price loop for the memecoins people are watching.

The full refresh reads discovery, forensics, GeckoTerminal and ratification, and
takes minutes. This loop only reads pool and curve accounts for a short list
(open Calls, then the coins that traded last cycle) and saves their prices apart
from the snapshot, so a slow cycle cannot leave them stale. It runs only while
someone has read a memecoin page recently, and spends from the price lane.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.db import connection
from runner_web.helius_discovery import Rpc, price_request
from runner_web.memecoin_chain_prices import chain_prices
from runner_web.memecoin_evidence import CreditBudgetReached
from runner_web.memecoin_store import FAST_PRICES_KEY, save_fast_quotes
from runner_web.memecoins import (
    MIN_CURVE_LIQUIDITY_USD,
    MIN_POOL_LIQUIDITY_USD,
    _market_states,
    _save_state,
    _time,
    memecoins_enabled,
    price_source,
)

LOG = logging.getLogger(__name__)
# A price read costs two credits, so this sets the daily spend while pages are read.
FAST_PRICE_SECONDS = max(15, int(os.getenv("MEMECOIN_FAST_PRICE_SECONDS", "60")))
# Nobody has read a memecoin page for this long: the loop idles and spends nothing.
FAST_ACTIVE_SECONDS = 300
WATCH_LIMIT = 40
KEEP_SECONDS = 3600


def _call_coin_ids() -> list[str]:
    with connection() as database:
        rows = database.execute(
            "SELECT coin_id FROM memecoin_calls WHERE status='active' "
            "UNION SELECT coin_id FROM memecoin_call_orders WHERE status='pending'"
        ).fetchall()
    return [str(row["coin_id"]) for row in rows]


def watched_rows(snapshot_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open Calls first, then the coins that traded last cycle, busiest first."""

    by_id = {row["id"]: row for row in snapshot_rows if isinstance(row, dict) and row.get("id")}
    by_pool = {row.get("pool_address"): row for row in by_id.values()}
    states = _market_states(keys=("memecoin_pool_watch", "memecoin_curve_watch"))
    traded = [
        by_pool[item["pool_address"]]
        for key in ("memecoin_curve_watch", "memecoin_pool_watch")
        for item in states.get(key) or []
        if isinstance(item, dict) and item.get("pool_address") in by_pool
    ]
    chosen: dict[str, dict[str, Any]] = {}
    for row in [*(by_id[i] for i in _call_coin_ids() if i in by_id), *traded]:
        if len(chosen) >= WATCH_LIMIT:
            break
        if isinstance(row.get("discovery"), dict):
            chosen.setdefault(row["id"], row)
    return list(chosen.values())


def viewed_recently(at: datetime) -> bool:
    seen = _time(_market_states(keys=("memecoin_last_view",)).get("memecoin_last_view"))
    return seen is not None and 0 <= (at - seen).total_seconds() <= FAST_ACTIVE_SECONDS


def refresh_watched_prices(*, at: datetime | None = None, rpc: Rpc | None = None) -> dict[str, Any]:
    current = at or datetime.now(UTC)
    if not memecoins_enabled() or price_source() not in ("chain", "shadow"):
        return {"status": "disabled"}
    if not viewed_recently(current):
        return {"status": "idle"}
    snapshot = _market_states(keys=("memecoins_snapshot",)).get("memecoins_snapshot") or {}
    rows = watched_rows(snapshot.get("rows") or [])
    if not rows:
        return {"status": "empty"}
    receipts = {row["pool_address"]: row["discovery"] for row in rows}
    curves = [r for r in receipts.values() if r.get("venue") == "bonding_curve"]
    pools = [r for r in receipts.values() if r.get("venue") != "bonding_curve"]
    try:
        chain = chain_prices(pools, curves, rpc=rpc or price_request)
    except CreditBudgetReached:
        return {"status": "budget"}
    except Exception:
        LOG.warning("Fast price read failed", exc_info=True)
        return {"status": "error"}
    saved = _market_states(keys=(FAST_PRICES_KEY,)).get(FAST_PRICES_KEY) or {}
    cutoff = current - timedelta(seconds=KEEP_SECONDS)
    prices = {
        coin_id: entry
        for coin_id, entry in (saved.get("prices") or {}).items()
        if (moment := _time(entry.get("observed_at"))) and moment > cutoff
    }
    quotes = []
    for row in rows:
        quote = chain["prices"].get(row["pool_address"])
        if not quote:
            continue
        minimum = (
            MIN_CURVE_LIQUIDITY_USD
            if row.get("venue") == "bonding_curve"
            else MIN_POOL_LIQUIDITY_USD
        )
        if quote["liquidity_usd"] < minimum:
            continue
        prices[row["id"]] = {
            "price": quote["price"],
            "liquidity_usd": quote["liquidity_usd"],
            "observed_at": current.isoformat(),
        }
        quotes.append({"coin_id": row["id"], "price": quote["price"]})
    _save_state(FAST_PRICES_KEY, {"prices": prices}, current)
    save_fast_quotes(quotes, observed_at=current)
    return {"status": "ok", "count": len(quotes)}
