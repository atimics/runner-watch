"""Real liquidity: twice the quote side actually in a pool, in dollars.

A pool's quoted liquidity values the coin at the coin's own price, so a pool
holding $47 of USDC against a coin priced at will can quote $941M. The RATi
Rules read the quote side instead. Pools the chain reader prices (PumpSwap)
already carry it; for the rest, DexScreener reports each pool's quote amount,
30 pools a request, free.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from ratitrust.memecoin import MIN_LIQUIDITY_USD

LOG = logging.getLogger(__name__)
PAIRS_URL = "https://api.dexscreener.com/latest/dex/pairs/solana/"
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

    Chain-priced rows already hold twice their quote side. Only pools quoting
    enough to pass are looked up: a pool quoting less cannot hold more.
    """

    wanted = []
    for row in rows:
        if row.get("venue") != "pool":
            continue
        if row.get("source") == CHAIN_SOURCE:
            row["real_liquidity_usd"] = row.get("liquidity_usd")
        elif float(row.get("liquidity_usd") or 0) >= MIN_LIQUIDITY_USD and row.get("pool_address"):
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
