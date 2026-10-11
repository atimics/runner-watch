"""Small, cached reads for a searched mint while the worker collects its history."""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

from runner_web.db import connection
from runner_web.helius_discovery import _decode, _encode, rpc_request
from runner_web.memecoin_chain_parser import clean_claim
from runner_web.memecoins import (
    SOLANA_ADDRESS,
    Download,
    _amount_label,
    _download,
    _lock_search_state,
    _number,
    _price_label,
    _state_dict,
    _time,
    _write_state,
    memecoins_enabled,
)
from runner_web.solana_keys import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, find_program_address

METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
CACHE_KEY = "memecoin_search_basics"
ATTEMPTS_KEY = "memecoin_basics_attempts"
CACHE_SECONDS = 300
RETRY_SECONDS = 60
MAX_CACHED = 100
MAX_ATTEMPTS = 20
LOOKUP_SECONDS = 3.0


def _claim(value: Any, limit: int) -> str:
    return clean_claim(value, limit) if isinstance(value, str) else ""


def _fact(label: str, value: Any, *, address: bool = False) -> dict[str, Any]:
    return {"label": label, "value": str(value), "address": address}


def _authority(label: str, info: dict[str, Any], key: str) -> dict[str, Any]:
    value = info.get(key)
    if key not in info or (
        value is not None
        and (
            not isinstance(value, str)
            or not SOLANA_ADDRESS.fullmatch(value)
            or len(_decode(value, 44)) != 32
        )
    ):
        return _fact(label, "Pending")
    return _fact(label, value or "Revoked", address=bool(value))


def _metadata(account: dict[str, Any] | None, mint: str) -> dict[str, Any] | None:
    if not account or account.get("owner") != METADATA_PROGRAM:
        return None
    try:
        data, encoding = account["data"]
        if encoding != "base64":
            return None
        raw = base64.b64decode(data, validate=True)
        if len(raw) < 65 or raw[0] != 4 or _encode(raw[33:65]) != mint:
            return None
        offset = 65

        def take(size: int) -> bytes:
            nonlocal offset
            if size < 0 or offset + size > len(raw):
                raise ValueError("Metadata data is incomplete")
            value = raw[offset : offset + size]
            offset += size
            return value

        def string(limit: int) -> str:
            size = int.from_bytes(take(4), "little")
            if size > limit:
                raise ValueError("Metadata field is too long")
            return take(size).decode("utf-8").rstrip("\0").strip()

        result = {"name": string(32), "symbol": string(10), "uri": string(200)}
        take(2)  # Seller fee basis points.
        creators = take(1)[0]
        if creators not in (0, 1):
            return None
        if creators:
            count = int.from_bytes(take(4), "little")
            if count > 5:
                return None
            take(count * 34)
        if take(1)[0] not in (0, 1):
            return None
        mutable = take(1)[0]
        if mutable not in (0, 1):
            return None
        return {
            **result,
            "mutable": bool(mutable),
            "update_authority": _encode(raw[1:33]),
            "authority_known": True,
        }
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def _chain_basics(mint: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") or {}
    accounts = result.get("value")
    if not isinstance(accounts, list) or len(accounts) != 2:
        raise ValueError("Mint read is incomplete")
    account = accounts[0]
    if account is None:
        return {"status": "invalid", "facts": []}
    program = account.get("owner")
    data = account.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        return {"status": "invalid", "facts": []}
    if not isinstance(parsed, dict):
        raise ValueError("Mint data is awaiting a parsed read")
    info = parsed.get("info") or {}
    if parsed.get("type") != "mint" or info.get("isInitialized") is False:
        return {"status": "invalid", "facts": []}
    decimals, supply = info.get("decimals"), info.get("supply")
    if (
        info.get("isInitialized") is not True
        or type(decimals) is not int
        or not 0 <= decimals <= 255
        or not isinstance(supply, str)
        or not supply.isascii()
        or not supply.isdigit()
        or not 0 <= int(supply) <= 2**64 - 1
    ):
        raise ValueError("Mint fields are incomplete")
    with localcontext() as context:
        context.prec = 300
        displayed_supply = format(Decimal(supply).scaleb(-decimals), "f")
    facts = [
        _fact("Token program", "Token-2022" if program == TOKEN_2022_PROGRAM else "SPL Token"),
        _fact("Supply", displayed_supply),
        _fact("Decimals", decimals),
        _authority("Mint authority", info, "mintAuthority"),
        _authority("Freeze authority", info, "freezeAuthority"),
    ]
    metadata = _metadata(accounts[1], mint)
    for extension in info.get("extensions") or []:
        state = extension.get("state") or {}
        kind = extension.get("extension")
        if kind == "tokenMetadata" and state.get("mint") == mint:
            authority = _authority("Metadata update authority", state, "updateAuthority")
            known = authority["value"] != "Pending"
            metadata = {
                "name": state.get("name"),
                "symbol": state.get("symbol"),
                "mutable": authority["address"] if known else None,
                "update_authority": state.get("updateAuthority") if known else None,
                "authority_known": known,
            }
        if kind == "metadataPointer":
            facts.append(_authority("Metadata pointer authority", state, "authority"))
    if metadata:
        mutable = metadata.get("mutable")
        facts.extend(
            [
                _fact(
                    "Metadata edits",
                    "Allowed" if mutable else "Locked" if mutable is False else "Pending",
                ),
                _fact(
                    "Metadata update authority",
                    (metadata.get("update_authority") or "Revoked")
                    if metadata.get("authority_known")
                    else "Pending",
                    address=bool(metadata.get("update_authority")),
                ),
            ]
        )
    else:
        facts.append(_fact("Metadata controls", "Pending"))
    extensions = [
        item.get("extension") for item in info.get("extensions") or [] if item.get("extension")
    ]
    if extensions:
        facts.append(_fact("Token extensions", ", ".join(extensions)))
    return {
        "status": "ready",
        "facts": facts,
        "metadata": metadata,
        "slot": (result.get("context") or {}).get("slot"),
    }


def _market_basics(mint: str, body: bytes) -> dict[str, Any]:
    token = json.loads(body).get("data") or {}
    attrs = token.get("attributes") or {}
    if token.get("id") != f"solana_{mint}" or attrs.get("address") != mint:
        raise ValueError("Market token differs from the searched mint")
    facts = []
    price = _number(attrs.get("price_usd"), minimum=0)
    if price is not None and price > 0:
        facts.append(_fact("Price · GeckoTerminal", _price_label(price)))
    for label, value in [
        ("24h volume", (attrs.get("volume_usd") or {}).get("h24")),
        ("Pool inventory value", attrs.get("total_reserve_in_usd")),
        ("Market cap", attrs.get("market_cap_usd")),
    ]:
        number = _number(value, minimum=0)
        if number is not None:
            facts.append(_fact(label, _amount_label(number)))
    return {
        "facts": facts,
        "name": _claim(attrs.get("name"), 160),
        "symbol": _claim(attrs.get("symbol"), 32),
    }


def _save_cache(database: Any, cache: dict[str, Any], at: datetime) -> None:
    newest = sorted(cache.items(), key=lambda item: item[1].get("at", ""), reverse=True)
    _write_state(database, CACHE_KEY, dict(newest[:MAX_CACHED]), at)


def lookup_memecoin(
    address: str,
    *,
    fetch: bool = True,
    at: datetime | None = None,
    rpc: Any = None,
    download: Download | None = None,
) -> dict[str, Any] | None:
    """One mint/metadata RPC and one market read, shared across web processes.

    The cache lease is saved before calling providers. The queue's admission
    rules and a shared attempt cap also apply to repeat lookups.
    """
    mint = address.strip()
    if not SOLANA_ADDRESS.fullmatch(mint) or len(_decode(mint, 44)) != 32:
        return None
    current = at or datetime.now(UTC)
    pending = {
        "address": mint,
        "status": "pending",
        "checked_at": None,
        "facts": [
            _fact(label, "Pending")
            for label in (
                "Token program",
                "Supply",
                "Decimals",
                "Mint authority",
                "Freeze authority",
                "Metadata controls",
            )
        ],
    }
    with connection() as database:
        _lock_search_state(database)
        cache = _state_dict(database, CACHE_KEY)
        entry = cache.get(mint) or {}
        until = _time(entry.get("until"))
        if until and current < until:
            return entry.get("value") or pending
        if not fetch or not memecoins_enabled():
            return entry.get("value")
        attempts = _state_dict(database, ATTEMPTS_KEY)
        recent = [
            stamp
            for stamp in attempts.get("times", [])
            if (moment := _time(stamp)) and 0 <= (current - moment).total_seconds() < 300
        ]
        if len(recent) >= MAX_ATTEMPTS:
            return entry.get("value") or pending
        _write_state(database, ATTEMPTS_KEY, {"times": [*recent, current.isoformat()]}, current)
        cache[mint] = {
            "at": current.isoformat(),
            "until": (current + timedelta(seconds=RETRY_SECONDS)).isoformat(),
            "value": entry.get("value") or pending,
        }
        _save_cache(database, cache, current)
    started = time.monotonic()
    value = {**pending, "checked_at": current.isoformat()}
    try:
        metadata = find_program_address(
            [b"metadata", _decode(METADATA_PROGRAM, 44), _decode(mint, 44)], METADATA_PROGRAM
        )
        payload = (rpc or rpc_request)(
            {
                "jsonrpc": "2.0",
                "id": "token-basics",
                "method": "getMultipleAccounts",
                "params": [[mint, metadata], {"encoding": "jsonParsed", "commitment": "finalized"}],
            },
            credits=1,
            timeout=2.0,
        )
        value.update(_chain_basics(mint, payload))
    except Exception:
        # Keep provider errors, which can contain credentials, out of the result.
        pass
    remaining = LOOKUP_SECONDS - (time.monotonic() - started)
    if value["status"] != "invalid" and remaining > 0:
        try:
            market = _market_basics(
                mint,
                (download or _download)(
                    f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}",
                    min(1.5, remaining),
                ),
            )
            value["facts"].extend(market["facts"])
            value.update(name=market["name"], symbol=market["symbol"])
        except Exception:
            pass
    metadata = value.pop("metadata", None) or {}
    value["name"] = _claim(metadata.get("name"), 160) or value.get("name")
    value["symbol"] = _claim(metadata.get("symbol"), 32) or value.get("symbol")
    ttl = CACHE_SECONDS if value["status"] == "ready" else RETRY_SECONDS
    with connection() as database:
        _lock_search_state(database)
        cache = _state_dict(database, CACHE_KEY)
        cache[mint] = {
            "at": current.isoformat(),
            "until": (current + timedelta(seconds=ttl)).isoformat(),
            "value": value,
        }
        _save_cache(database, cache, current)
    return value
