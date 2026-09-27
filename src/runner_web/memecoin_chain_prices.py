"""Price and liquidity read from the pools themselves, through Helius.

A PumpSwap pool records its two token vaults and a virtual quote reserve
carried over from the bonding curve at graduation; a Pump bonding curve stores
its reserves directly. Checked against GeckoTerminal at the same moment on
2026-09-26: pools within 3%, curves within 0.4%, and GeckoTerminal lags.

    pool price  = (quote vault + virtual quote reserves) / base vault
    curve price = virtual SOL reserves / virtual token reserves
    liquidity   = 2 x the real quote side, the money actually in the pool

Only PumpSwap pools and Pump curves are read here; other pools keep their
GeckoTerminal quote.
"""

from __future__ import annotations

import base64
import binascii
import struct
from collections.abc import Callable
from typing import Any

from runner_web.helius_discovery import _encode

PUMP_SWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Raydium AMM v4 SOL-USDC vaults: within 0.13% of GeckoTerminal's SOL price.
SOL_VAULT = "DQyrAcCrDXQ7NeoqGgDCZwBvWDcYmFCjSb9JtteuvPpz"
USDC_VAULT = "HLmqeL62xR1QoZ1HKKbXRrdN1p3phKpxRMb2VVopvBBz"
POOL_DISCRIMINATOR = bytes([241, 154, 109, 4, 17, 177, 109, 188])
CURVE_DISCRIMINATOR = bytes([23, 183, 248, 55, 96, 216, 172, 96])
ACCOUNTS_PER_CALL = 100
PUMP_TOKEN_DECIMALS = 6
SOL_DECIMALS = 9

Rpc = Callable[..., dict[str, Any]]


def read_accounts(addresses: list[str], rpc: Rpc) -> dict[str, dict[str, Any] | None]:
    """jsonParsed accounts in calls of 100, one credit each."""

    found: dict[str, dict[str, Any] | None] = {}
    unique = list(dict.fromkeys(addresses))
    for offset in range(0, len(unique), ACCOUNTS_PER_CALL):
        chunk = unique[offset : offset + ACCOUNTS_PER_CALL]
        payload = rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getMultipleAccounts",
                "params": [chunk, {"encoding": "jsonParsed", "commitment": "confirmed"}],
            },
            credits=1,
        )
        values = (payload.get("result") or {}).get("value")
        if not isinstance(values, list) or len(values) != len(chunk):
            raise ValueError("Invalid account batch")
        found.update(zip(chunk, values, strict=True))
    return found


def _raw(account: dict[str, Any] | None) -> bytes | None:
    """Program-owned data, which jsonParsed returns as base64."""

    data = (account or {}).get("data")
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
        return None
    try:
        return base64.b64decode(data[0], validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None


def token_amount(account: dict[str, Any] | None) -> tuple[float, int] | None:
    try:
        amount = account["data"]["parsed"]["info"]["tokenAmount"]  # type: ignore[index]
        return int(amount["amount"]) / 10 ** int(amount["decimals"]), int(amount["decimals"])
    except (KeyError, TypeError, ValueError):
        return None


def _pool_layout(raw: bytes | None) -> dict[str, Any] | None:
    # discriminator, bump u8, index u16, creator, base mint, quote mint, lp mint,
    # base vault, quote vault, lp supply u64, coin creator, two bools, i128.
    if raw is None or len(raw) < 8 + 3 + 32 * 6 + 8 + 32 + 2 + 16 or raw[:8] != POOL_DISCRIMINATOR:
        return None
    offset = 8 + 3 + 32
    keys = [_encode(raw[offset + 32 * index : offset + 32 * (index + 1)]) for index in range(5)]
    offset += 32 * 5 + 8 + 32 + 2
    virtual = int.from_bytes(raw[offset : offset + 16], "little", signed=True)
    return {
        "base_mint": keys[0],
        "quote_mint": keys[1],
        "base_vault": keys[3],
        "quote_vault": keys[4],
        "virtual_quote": virtual,
    }


def chain_prices(
    pools: list[dict[str, Any]], curves: list[dict[str, Any]], *, rpc: Rpc
) -> dict[str, Any]:
    """Chain price and liquidity by pool address, for PumpSwap pools and Pump curves.

    Pools are read in two passes (pool account, then its vaults); curves and
    the SOL price in the first. A pool that does not decode, or whose quote is
    neither SOL nor USDC, is left out and keeps its GeckoTerminal quote.
    """

    pumpswap = [pool for pool in pools if pool.get("program") in (None, PUMP_SWAP)]
    first = read_accounts(
        [SOL_VAULT, USDC_VAULT]
        + [pool["pool_address"] for pool in pumpswap]
        + [curve["pool_address"] for curve in curves],
        rpc,
    )
    sol, usdc = token_amount(first[SOL_VAULT]), token_amount(first[USDC_VAULT])
    if not sol or not usdc or sol[0] <= 0:
        raise ValueError("SOL price unavailable")
    sol_usd = usdc[0] / sol[0]
    prices: dict[str, dict[str, Any]] = {}
    for curve in curves:
        raw = _raw(first.get(curve["pool_address"]))
        if raw is None or len(raw) < 8 + 41 or raw[:8] != CURVE_DISCRIMINATOR:
            continue
        virtual_token, virtual_sol, _, real_sol = struct.unpack_from("<QQQQ", raw, 8)
        if virtual_token <= 0 or raw[48]:
            continue  # complete: the curve has graduated and no longer trades
        price_sol = (virtual_sol / 10**SOL_DECIMALS) / (virtual_token / 10**PUMP_TOKEN_DECIMALS)
        prices[curve["pool_address"]] = {
            "price": price_sol * sol_usd,
            "liquidity_usd": real_sol / 10**SOL_DECIMALS * sol_usd,
        }
    layouts = {
        pool["pool_address"]: layout
        for pool in pumpswap
        if (layout := _pool_layout(_raw(first.get(pool["pool_address"]))))
        and layout["base_mint"] == pool["token_address"]
        and layout["quote_mint"] in (SOL, USDC)
    }
    vaults = read_accounts(
        [
            key
            for layout in layouts.values()
            for key in (layout["base_vault"], layout["quote_vault"])
        ],
        rpc,
    )
    for address, layout in layouts.items():
        base, quote = (
            token_amount(vaults[layout["base_vault"]]),
            token_amount(vaults[layout["quote_vault"]]),
        )
        if not base or not quote or base[0] <= 0:
            continue
        quote_usd = sol_usd if layout["quote_mint"] == SOL else 1.0
        virtual = layout["virtual_quote"] / 10 ** quote[1]
        prices[address] = {
            "price": (quote[0] + virtual) / base[0] * quote_usd,
            "liquidity_usd": 2 * quote[0] * quote_usd,
        }
    return {"sol_usd": sol_usd, "prices": prices}
