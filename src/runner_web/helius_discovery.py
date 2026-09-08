"""Bounded PumpSwap discovery from finalized Helius transaction history."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from runner_watch.xml_security import read_limited

RPC_URL = "https://mainnet.helius-rpc.com/"
PUMP_SWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
# pump-fun/pump-public-docs, idl/pump_amm.json: create_pool.
CREATE_POOL = bytes([233, 146, 209, 142, 207, 104, 64, 188])
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MAX_PAGES = 2
MAX_BYTES = 16 * 1024 * 1024
Rpc = Callable[[dict[str, Any]], dict[str, Any]]


def _decode(value: Any, limit: int = 1024) -> bytes:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError("Invalid base58 field")
    number = 0
    for char in value:
        number = number * 58 + BASE58.index(char)
    prefix = len(value) - len(value.lstrip("1"))
    return b"\0" * prefix + number.to_bytes((number.bit_length() + 7) // 8, "big")


def _encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58[remainder] + encoded
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + encoded


def _address(value: Any) -> str:
    if len(_decode(value, 44)) != 32:
        raise ValueError("Invalid Solana address")
    return value


def rpc_request(body: dict[str, Any]) -> dict[str, Any]:
    key = os.getenv("HELIUS_API_KEY", "").strip()
    if not key:
        raise ValueError("HELIUS_API_KEY is required for Solana discovery")
    from runner_web.memecoin_evidence import reserve_credits

    options = body.get("params", [None, {}])[1]
    limit = options.get("limit", 100)
    reserve_credits(max(10, ((limit + 99) // 100) * 10))
    request = urllib.request.Request(
        RPC_URL + "?" + urllib.parse.urlencode({"api-key": key}),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "RATi/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(read_limited(response, max_bytes=MAX_BYTES))
    except Exception:
        # Provider errors and HTTP exception URLs can contain the API key.
        raise ValueError("Helius request failed") from None
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("Helius RPC returned an error")
    return payload


def pool_creations(entries: list[Any], *, at: datetime) -> list[dict[str, Any]]:
    pools = {}
    for entry in entries:
        try:
            meta = entry["meta"]
            if meta["err"] is not None:
                continue
            transaction = entry["transaction"]
            signature = transaction["signatures"][0]
            if len(_decode(signature, 88)) != 64:
                continue
            block_time = entry["blockTime"]
            slot = entry["slot"]
            if (
                type(block_time) is not int
                or type(slot) is not int
                or slot < 0
                or not 0 <= at.timestamp() - block_time <= 86400
            ):
                continue
            instructions = list(transaction["message"]["instructions"])
            for group in meta.get("innerInstructions") or []:
                instructions.extend(group["instructions"])
            for instruction in instructions:
                try:
                    if instruction.get("programId") != PUMP_SWAP:
                        continue
                    data = _decode(instruction.get("data"))
                    if len(data) < 58 or data[:8] != CREATE_POOL:
                        continue
                    accounts = instruction["accounts"]
                    pool = _address(accounts[0])
                    base = _address(accounts[3])
                    quote = _address(accounts[4])
                    if base == quote:
                        continue
                    pools.setdefault(
                        pool,
                        {
                            "pool_address": pool,
                            "pool_creator": _address(accounts[2]),
                            "declared_creator": _encode(data[26:58]),
                            "token_address": base,
                            "quote_address": quote,
                            "network": "solana",
                            "signature": signature,
                            "slot": slot,
                            "created_at": datetime.fromtimestamp(block_time, UTC).isoformat(),
                            "source_url": f"https://solscan.io/tx/{signature}",
                            "commitment": "finalized",
                            "program": PUMP_SWAP,
                        },
                    )
                except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                    continue
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            continue
    return list(pools.values())


def discover_pools(*, at: datetime, rpc: Rpc | None = None) -> dict[str, Any]:
    call = rpc or rpc_request
    limit = max(1, min(int(os.getenv("HELIUS_DISCOVERY_TX_LIMIT", "100")), 1000))
    pools = {}
    cursor = None
    transactions = []
    received = 0
    pages = 0
    for page in range(MAX_PAGES):
        options = {
            "transactionDetails": "full",
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "commitment": "finalized",
            "sortOrder": "desc",
            "limit": limit,
            "filters": {"status": "succeeded", "blockTime": {"gte": int(at.timestamp()) - 86400}},
        }
        if cursor:
            options["paginationToken"] = cursor
        payload = call(
            {
                "jsonrpc": "2.0",
                "id": page + 1,
                "method": "getTransactionsForAddress",
                "params": [PUMP_SWAP, options],
            }
        )
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError("Helius RPC returned an error")
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise ValueError("Helius transaction response is invalid")
        entries = result["data"]
        if len(entries) > limit:
            raise ValueError("Helius transaction response exceeds the limit")
        transactions.extend(entries)
        received += len(entries)
        pages += 1
        for pool in pool_creations(entries, at=at):
            pools.setdefault(pool["pool_address"], pool)
        next_cursor = result.get("paginationToken")
        if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 128):
            raise ValueError("Helius pagination token is invalid")
        if next_cursor and next_cursor == cursor:
            raise ValueError("Helius pagination stalled")
        cursor = next_cursor
        if not cursor:
            break
    return {
        "pools": list(pools.values()),
        "transactions": transactions,
        "received_transactions": received,
        "pages": pages,
        "page_limit": limit,
        "partial": bool(cursor),
    }
