"""Real liquidity: twice the quote side actually in a pool, in dollars.

A coin can trade in several pools, a Trebuchet launch in one per hub pair:
DexScreener's token lookup lists them all, 30 coins a request, and each is
valued the same way.

A pool's quoted liquidity values the coin at the coin's own price, so a pool
holding $47 of USDC against a coin priced at will can quote $941M. The RATi
Rules read the quote side instead. Pools the chain reader prices (PumpSwap)
already carry it; for the rest, DexScreener reports each pool's quote amount,
30 pools a request, free: a few requests a cycle.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

LOG = logging.getLogger(__name__)
PAIRS_URL = "https://api.dexscreener.com/latest/dex/pairs/solana/"
TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana/"
# Dust pools are left out; past the busiest few, the rest share one wedge.
POOL_FLOOR_USD = 100.0
MAX_POOLS = 6
CHAIN_SOURCE = "Solana (Helius)"
BATCH = 30
Download = Callable[[str, float], bytes]


def quote_side_usd(pair: dict[str, Any]) -> float | None:
    """2 × quote amount × the quote token's dollar price, from one DexScreener pair."""

    try:
        quote = float(pair["liquidity"]["quote"])
        # priceUsd is the coin's dollar price and priceNative its price in the
        # quote token, so their ratio is the quote token's dollar price.
        quote_usd = float(pair["priceUsd"]) / float(pair["priceNative"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return 2 * quote * quote_usd if quote >= 0 and quote_usd > 0 else None


def attach_real_liquidity(rows: list[dict[str, Any]], *, download: Download) -> None:
    """Set `real_liquidity_usd` on each pool row; unread pools are left without it.

    Chain-priced rows already hold twice their quote side. Every other pool is
    looked up, whatever it quotes: a concentrated pool can hold more on its
    quote side than its quoted total, so the quote is no bound either way.
    """

    wanted = []
    for row in rows:
        if row.get("venue") != "pool":
            continue
        if row.get("source") == CHAIN_SOURCE:
            row["real_liquidity_usd"] = row.get("liquidity_usd")
        elif row.get("pool_address"):
            wanted.append(row)
    by_pool = {row["pool_address"]: row for row in wanted}
    addresses = list(by_pool)
    for offset in range(0, len(addresses), BATCH):
        chunk = addresses[offset : offset + BATCH]
        try:
            pairs = json.loads(download(PAIRS_URL + ",".join(chunk), 10.0)).get("pairs") or []
        except Exception:
            LOG.warning("DexScreener pair read failed for %d pools", len(chunk), exc_info=True)
            continue
        for pair in pairs:
            row = by_pool.get(str(pair.get("pairAddress") or ""))
            base = str((pair.get("baseToken") or {}).get("address") or "")
            if row is None or base != row.get("token_address"):
                continue  # only the pool and coin we asked about
            value = quote_side_usd(pair)
            if value is not None:
                row["real_liquidity_usd"] = value


def attach_pools(rows: list[dict[str, Any]], *, download: Download) -> None:
    """Set `pools` on each pool row: every pool trading the coin, busiest first.

    Only pairs with the coin as base count; the board's own pool keeps the
    real liquidity already read for it. Unread coins are left without a list.
    """

    by_token = {
        str(row["token_address"]): row
        for row in rows
        if row.get("venue") == "pool" and row.get("token_address")
    }
    tokens = list(by_token)
    for offset in range(0, len(tokens), BATCH):
        chunk = tokens[offset : offset + BATCH]
        try:
            pairs = json.loads(download(TOKENS_URL + ",".join(chunk), 10.0))
        except Exception:
            LOG.warning("DexScreener token read failed for %d coins", len(chunk), exc_info=True)
            continue
        found: dict[str, dict[str, dict[str, Any]]] = {}
        for pair in pairs if isinstance(pairs, list) else []:
            if not isinstance(pair, dict):
                continue
            token = str((pair.get("baseToken") or {}).get("address") or "")
            address = str(pair.get("pairAddress") or "")
            row = by_token.get(token)
            if row is None or not address:
                continue
            real = quote_side_usd(pair)
            if address == row.get("pool_address") and row.get("real_liquidity_usd") is not None:
                real = row["real_liquidity_usd"]
            try:
                quoted = float((pair.get("liquidity") or {}).get("usd"))
            except (TypeError, ValueError):
                quoted = None
            found.setdefault(token, {})[address] = {
                "address": address,
                "dex": str(pair.get("dexId") or ""),
                "quote": str((pair.get("quoteToken") or {}).get("symbol") or ""),
                "real_liquidity_usd": real,
                "liquidity_usd": quoted,
            }
        for token in chunk:
            if token in found:
                by_token[token]["pools"] = _busiest(list(found[token].values()))


def _busiest(pools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = sorted(
        (
            pool
            for pool in pools
            if pool["real_liquidity_usd"] is not None
            and pool["real_liquidity_usd"] >= POOL_FLOOR_USD
        ),
        key=lambda pool: (-pool["real_liquidity_usd"], pool["address"]),
    )
    if len(kept) <= MAX_POOLS:
        return kept
    rest = kept[MAX_POOLS - 1 :]
    return kept[: MAX_POOLS - 1] + [
        {
            "address": None,
            "dex": "others",
            "quote": "",
            "real_liquidity_usd": sum(pool["real_liquidity_usd"] for pool in rest),
            "liquidity_usd": sum(pool["liquidity_usd"] or 0 for pool in rest),
            "count": len(rest),
        }
    ]
