"""Ratified: a memecoin that meets RATi's basic standards.

Seven standards, all required. Not an endorsement or advice: it says the
token's controls, pool, age, record and holders pass basic checks.

The mint account (one credit per 100) answers the three control standards;
a revoked authority cannot come back and Token-2022 extensions are fixed at
creation, so a clean answer is kept. Top holders cost a credit a coin and are
read only for coins that pass everything else, every six hours.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from runner_web.helius_discovery import _encode
from runner_web.memecoin_chain_prices import (
    POOL_DISCRIMINATOR,
    PUMP_SWAP,
    Rpc,
    _raw,
    read_accounts,
)
from runner_web.ratification import summarize
from runner_web.solana_keys import is_program_address

LOG = logging.getLogger(__name__)
MIN_LIQUIDITY_USD = 10_000.0
MIN_HOLDERS = 100
# The pool's liquidity tokens still in existence; the rest were burned.
MAX_LP_LEFT_PCT = 10.0
MAX_COUNT_LOOKUPS = 5
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
CPMM_DISCRIMINATOR = bytes([247, 237, 227, 245, 215, 195, 222, 70])
# (program, discriminator, lp mint offset, lp supply offset, name): Pump's and
# Raydium's official IDLs. Pump burns a graduated pool's liquidity tokens.
LP_LAYOUTS = (
    (PUMP_SWAP, POOL_DISCRIMINATOR, 107, 203, "PumpSwap"),
    (RAYDIUM_CPMM, CPMM_DISCRIMINATOR, 136, 333, "Raydium CPMM"),
)
MIN_AGE = timedelta(hours=24)
MAX_TOP10_PCT = 30.0
HOLDERS_TTL = timedelta(hours=6)
# Bump when the holder count changes, so answers under older rules are redone.
HOLDER_RULES = 5
MAX_HOLDER_READS = 20
# getTokenLargestAccounts lists this many accounts.
LARGEST_LISTED = 20
# Token-2022 features that let someone tax, block, seize or pause holders.
RISKY_EXTENSIONS = {
    "transferFeeConfig",
    "transferHook",
    "permanentDelegate",
    "pausableConfig",
    "defaultAccountState",
    "nonTransferable",
    "confidentialTransferMint",
}
# Pools and curves hold supply for everyone, so their accounts are left out of
# the top holders. An account owned by a program address is left out only when
# that address belongs to one of these programs: anyone can hold supply under a
# program of their own, and that must count.
POOL_PROGRAMS = {
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump bonding curves
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",  # Raydium LaunchLab
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpools
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora DAMM v1
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",  # Meteora DAMM v2
}
# Pool authorities with no account of their own to show a program.
POOL_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM
}
RISK_KINDS = {
    "synchronized_buys": "launch bundle",
    "creator_sell": "creator selling",
    "liquidity_withdrawal": "liquidity pulled",
}
LABELS = {
    "mint_authority": "Mint authority revoked",
    "freeze_authority": "Freeze authority revoked",
    "token_features": "No risky token features",
    "pool": "Graduated pool with $10K+ real liquidity",
    "age": "24 hours since its pool opened",
    "record": "No launch bundle, creator selling or liquidity pull",
    "holders": "Top 10 holders own 30% or less",
    "holder_count": "At least 100 holders",
    "liquidity_lock": "Pool liquidity burned or locked",
}
NOTE = (
    "Ratified: meets RATi's nine basic standards for a memecoin. "
    "Not an endorsement, a guarantee or advice."
)


def mint_controls(
    mints: list[str], known: dict[str, Any], *, rpc: Rpc
) -> dict[str, dict[str, Any]]:
    """Authorities, risky extensions and supply for each mint, reusing clean answers."""

    controls = {mint: known[mint] for mint in mints if (known.get(mint) or {}).get("clean")}
    unknown = [mint for mint in mints if mint not in controls]
    if unknown:
        for mint, account in read_accounts(unknown, rpc).items():
            try:
                info = account["data"]["parsed"]["info"]  # type: ignore[index]
            except (KeyError, TypeError):
                continue
            risky = sorted(
                str(extension.get("extension"))
                for extension in info.get("extensions") or []
                if extension.get("extension") in RISKY_EXTENSIONS
            )
            entry = {
                "mint_authority": info.get("mintAuthority"),
                "freeze_authority": info.get("freezeAuthority"),
                "risky_extensions": risky,
                "supply": int(info.get("supply") or 0) / 10 ** int(info.get("decimals") or 0),
            }
            entry["clean"] = not (
                entry["mint_authority"] or entry["freeze_authority"] or entry["risky_extensions"]
            )
            controls[mint] = entry
    return controls


def _amount(item: dict[str, Any]) -> float:
    """The raw amount scaled by decimals; uiAmount can be null and read as zero."""

    try:
        return int(item["amount"]) / 10 ** int(item["decimals"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Holder amount missing") from None


def top10_share(mint: str, *, supply: float, exclude: set[str], rpc: Rpc) -> float | None:
    """Percent of supply held by the ten largest wallets.

    Accounts held by a known pool or bonding curve are left out, found from the
    owner of each of the top 20 accounts and the program behind that owner (two
    more one-credit reads). Anything else counts, including supply held under
    a program nobody here knows.
    """

    payload = rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [mint, {"commitment": "confirmed"}],
        },
        credits=1,
    )
    accounts = [
        item
        for item in ((payload.get("result") or {}).get("value")) or []
        if isinstance(item, dict) and item.get("address") and item["address"] not in exclude
    ]
    if not accounts or supply <= 0:
        return None
    token_accounts = read_accounts([item["address"] for item in accounts], rpc)
    owners: dict[str, str | None] = {}
    for item in accounts:
        try:
            owners[item["address"]] = token_accounts[item["address"]]["data"]["parsed"]["info"][
                "owner"
            ]  # type: ignore[index]
        except (KeyError, TypeError):
            owners[item["address"]] = None  # unknown owners count as holders
    # Which program each program-address owner belongs to: one more read.
    derived = sorted({o for o in owners.values() if o and is_program_address(o)} - POOL_AUTHORITIES)
    programs = {
        address: (account or {}).get("owner")
        for address, account in (read_accounts(derived, rpc) if derived else {}).items()
    }
    held = []
    for item in accounts:
        owner = owners[item["address"]]
        if owner in POOL_AUTHORITIES or programs.get(owner or "") in POOL_PROGRAMS:
            continue
        held.append(_amount(item))
    held.sort(reverse=True)
    if len(held) < 10 and len(accounts) >= LARGEST_LISTED:
        # The largest wallets sit below the listed accounts, so none holds more
        # than the smallest listed: count that as an upper bound for each.
        smallest = min(_amount(item) for item in accounts)
        held += [smallest] * (10 - len(held))
    return sum(held[:10]) / supply * 100


def _age(row: dict[str, Any], at: datetime) -> timedelta | None:
    try:
        opened = datetime.fromisoformat(str(row["pool_created_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return at - opened


def _flags(row: dict[str, Any], bundled: bool) -> list[str]:
    factors = ((row.get("memecoin_assessment") or {}).get("risk") or {}).get("factors") or []
    found = {
        RISK_KINDS[factor["kind"]]
        for factor in factors
        if isinstance(factor, dict) and factor.get("kind") in RISK_KINDS
    }
    return sorted(found | ({"launch bundle"} if bundled else set()))


def _passes_free_standards(row: dict[str, Any], bundled: bool, at: datetime) -> bool:
    """Pool, age and record: known without a read, so they decide who is worth one."""

    age = _age(row, at)
    return (
        row.get("venue") == "pool"
        and float(row.get("liquidity_usd") or 0) >= MIN_LIQUIDITY_USD
        and age is not None
        and age >= MIN_AGE
        and not _flags(row, bundled)
    )


def liquidity_locks(pools: list[str], *, rpc: Rpc) -> dict[str, dict[str, Any]]:
    """How much of each pool's liquidity-token supply is still out, by pool.

    A pool keeps its own count of liquidity tokens issued; the mint's supply is
    what has not been burned. Pools on other DEXes come back with no answer.
    """

    results: dict[str, dict[str, Any]] = {}
    layouts = {}
    for pool, account in read_accounts(pools, rpc).items():
        raw = _raw(account)
        for program, discriminator, mint_at, supply_at, name in LP_LAYOUTS:
            if (
                raw is not None
                and (account or {}).get("owner") == program
                and raw[:8] == discriminator
                and len(raw) >= supply_at + 8
            ):
                issued = int.from_bytes(raw[supply_at : supply_at + 8], "little")
                layouts[pool] = (_encode(raw[mint_at : mint_at + 32]), issued, name)
                break
        else:
            results[pool] = {"left_pct": None, "dex": None}
    mints = read_accounts([mint for mint, _, _ in layouts.values()], rpc) if layouts else {}
    for pool, (lp_mint, issued, name) in layouts.items():
        try:
            outstanding = int(mints[lp_mint]["data"]["parsed"]["info"]["supply"])  # type: ignore[index]
        except (KeyError, TypeError, ValueError):
            results[pool] = {"left_pct": None, "dex": name}
            continue
        left = min(100.0, outstanding / issued * 100) if issued > 0 else None
        results[pool] = {"left_pct": left, "dex": name}
    return results


def holder_counts(
    mints: list[str],
    cached: dict[str, Any],
    *,
    download: Callable[[str, float], bytes],
    pause: float,
    at: datetime,
) -> dict[str, Any]:
    """Holder counts from GeckoTerminal's token info (free), a few a cycle, kept six hours."""

    counts = {
        mint: entry
        for mint, entry in cached.items()
        if at - datetime.fromisoformat(entry["checked_at"]) <= HOLDERS_TTL
    }
    looked = 0
    for mint in mints:
        if mint in counts or looked >= MAX_COUNT_LOOKUPS:
            continue
        time.sleep(pause)
        looked += 1
        url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{quote(mint)}/info"
        try:
            holders = json.loads(download(url, 10.0))["data"]["attributes"]["holders"]
            count = int(holders["count"])
        except Exception:
            LOG.warning("Holder count unavailable for %s", mint, exc_info=True)
            continue
        counts[mint] = {"checked_at": at.isoformat(), "count": count}
    return counts


def standards(
    row: dict[str, Any],
    controls: dict[str, Any] | None,
    top10_pct: float | None,
    bundled: bool,
    at: datetime,
    *,
    holder_count: int | None = None,
    lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Each standard as met, not met or not yet known, and whether all are met."""

    flagged = _flags(row, bundled)
    age = _age(row, at)
    liquidity = float(row.get("liquidity_usd") or 0)
    results = {
        "mint_authority": None if controls is None else not controls["mint_authority"],
        "freeze_authority": None if controls is None else not controls["freeze_authority"],
        "token_features": None if controls is None else not controls["risky_extensions"],
        "pool": row.get("venue") == "pool" and liquidity >= MIN_LIQUIDITY_USD,
        "age": None if age is None else age >= MIN_AGE,
        "record": not flagged,
        "holders": None if top10_pct is None else top10_pct <= MAX_TOP10_PCT,
        "holder_count": None if holder_count is None else holder_count >= MIN_HOLDERS,
        "liquidity_lock": None
        if not lock or lock.get("left_pct") is None
        else lock["left_pct"] <= MAX_LP_LEFT_PCT,
    }
    details = {
        "mint_authority": "still active" if controls and controls["mint_authority"] else "",
        "freeze_authority": "still active" if controls and controls["freeze_authority"] else "",
        "token_features": ", ".join(controls["risky_extensions"]) if controls else "",
        "pool": "on its bonding curve"
        if row.get("venue") == "bonding_curve"
        else f"${liquidity:,.0f} real liquidity",
        "age": f"{age.total_seconds() / 3600:.0f} hours" if age is not None else "",
        "record": ", ".join(flagged),
        "holders": f"top 10 hold {top10_pct:.0f}%" if top10_pct is not None else "",
        "holder_count": f"{holder_count:,} holders" if holder_count is not None else "",
        "liquidity_lock": _lock_detail(lock),
    }
    return summarize(LABELS, results, details, note=NOTE, as_of=at.isoformat())


def _lock_detail(lock: dict[str, Any] | None) -> str:
    if not lock:
        return ""
    if lock.get("left_pct") is None:
        return (
            f"{lock['dex']} pool not readable"
            if lock.get("dex")
            else "this DEX is not supported yet"
        )
    if lock["left_pct"] <= 0:
        return f"all {lock['dex']} liquidity tokens burned"
    return f"{lock['left_pct']:.0f}% of {lock['dex']} liquidity tokens still held"


def ratify_rows(
    rows: list[dict[str, Any]],
    saved: dict[str, Any],
    *,
    vaults: dict[str, str],
    bundled: set[str],
    rpc: Rpc,
    at: datetime,
    download: Callable[[str, float], bytes] | None = None,
    pause: float = 0.0,
) -> dict[str, Any]:
    """Attach `ratification` to every row; read the chain only for close candidates.

    `vaults` maps a pool address to its own token account; `bundled` holds the
    mints a launch-bundle check flagged. Returns the saved state for next cycle.
    """

    known_controls = dict(saved.get("controls") or {})
    holders = {
        mint: entry
        for mint, entry in (saved.get("holders") or {}).items()
        if entry.get("rules") == HOLDER_RULES
        and at - datetime.fromisoformat(entry["checked_at"]) <= HOLDERS_TTL
    }
    # The free standards first: only coins that pass them cost a read.
    candidates = [
        row
        for row in rows
        if row.get("token_address")
        and _passes_free_standards(row, row["token_address"] in bundled, at)
    ]
    controls = mint_controls([row["token_address"] for row in candidates], known_controls, rpc=rpc)
    known_controls.update(controls)
    try:
        locks = liquidity_locks([row["pool_address"] for row in candidates], rpc=rpc)
    except Exception:
        LOG.warning("Liquidity lock reads failed", exc_info=True)
        locks = {}
    reads = 0
    for row in candidates:
        mint = row["token_address"]
        control = controls.get(mint)
        if not control or not control["clean"] or mint in holders or reads >= MAX_HOLDER_READS:
            continue
        reads += 1
        try:
            share = top10_share(
                mint,
                supply=control["supply"],
                exclude={vault for vault in (vaults.get(row.get("pool_address", "")),) if vault},
                rpc=rpc,
            )
        except ValueError:
            continue  # this coin's holders stay unknown; the others still count
        holders[mint] = {"checked_at": at.isoformat(), "top10_pct": share, "rules": HOLDER_RULES}
    # The holder count is the last and a free lookup: only coins passing the rest.
    finalists = [
        row["token_address"]
        for row in candidates
        if (controls.get(row["token_address"]) or {}).get("clean")
        and (holders.get(row["token_address"]) or {}).get("top10_pct") is not None
        and holders[row["token_address"]]["top10_pct"] <= MAX_TOP10_PCT
        and (locks.get(row["pool_address"]) or {}).get("left_pct") is not None
        and locks[row["pool_address"]]["left_pct"] <= MAX_LP_LEFT_PCT
    ]
    counts = (
        holder_counts(finalists, saved.get("counts") or {}, download=download, pause=pause, at=at)
        if download
        else dict(saved.get("counts") or {})
    )
    for row in rows:
        mint = row.get("token_address")
        row["ratification"] = standards(
            row,
            known_controls.get(mint),
            (holders.get(mint) or {}).get("top10_pct"),
            mint in bundled,
            at,
            holder_count=(counts.get(mint) or {}).get("count"),
            lock=locks.get(row.get("pool_address", "")),
        )
    return {"controls": known_controls, "holders": holders, "counts": counts}
