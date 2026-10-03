"""Ratified memecoins: the chain reads the RATi Rules need, and the rules applied.

The standards are the RATi Rules' own (the vendored `ratitrust` package, at the
version in force); this module reads the facts they need, cheaply:

The mint account (one credit per 100) answers the three control standards;
a revoked authority cannot come back and Token-2022 extensions are fixed at
creation, so a clean answer is kept. Top holders cost a credit a coin and are
read only for coins that pass the free standards, every six hours; the holder
count (10 credits) only for coins passing everything else.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ratitrust.memecoin import (
    HOLDER_PAGE,
    LARGEST_LISTED,
    MAX_LP_LEFT_PCT,
    MAX_TOP10_PCT,
    POOL_AUTHORITIES,
    classify_mint,
    lp_left_pct,
    passes_free_standards,
    standards,
    top10_share_from,
)
from runner_web.helius_discovery import _encode
from runner_web.memecoin_chain_prices import (
    POOL_DISCRIMINATOR,
    PUMP_SWAP,
    Rpc,
    _raw,
    read_accounts,
)
from runner_web.solana_keys import is_program_address

__all__ = ["LARGEST_LISTED", "ratify_rows", "standards", "top10_share"]

LOG = logging.getLogger(__name__)
MAX_COUNT_LOOKUPS = 5
RAYDIUM_CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
CPMM_DISCRIMINATOR = bytes([247, 237, 227, 245, 215, 195, 222, 70])
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
# AMM v4's AmmInfo is not an Anchor account: no discriminator, 752 bytes.
AMM_V4_SIZE = 752
# (program, discriminator or None, lp mint offset, lp issued offset, name): Pump's
# and Raydium's official IDLs, and AMM v4's AmmInfo (lp_mint at 464, lp_amount at
# 720), checked against live pools. Pump burns a graduated pool's liquidity tokens.
# Concentrated pools (Raydium CLMM, Orca, Meteora DLMM) hold positions, not
# liquidity tokens: they are not read.
LP_LAYOUTS = (
    (PUMP_SWAP, POOL_DISCRIMINATOR, 107, 203, "PumpSwap"),
    (RAYDIUM_CPMM, CPMM_DISCRIMINATOR, 136, 333, "Raydium CPMM"),
    (RAYDIUM_AMM_V4, None, 464, 720, "Raydium AMM v4"),
)
HOLDERS_TTL = timedelta(hours=6)
LOCKS_TTL = timedelta(hours=1)
# Bump when the holder rules change, so answers under older rules are redone.
HOLDER_RULES = 6
MAX_HOLDER_READS = 20
# The row fields the rules read, kept with each recorded result.
ROW_FACTS = (
    "venue",
    "pool_address",
    "liquidity_usd",
    "real_liquidity_usd",
    "pool_created_at",
    "memecoin_assessment",
)


def mint_controls(
    mints: list[str], known: dict[str, Any], *, rpc: Rpc
) -> dict[str, dict[str, Any]]:
    """Authorities, risky extensions and supply for each mint, reusing clean answers."""

    controls = {mint: known[mint] for mint in mints if (known.get(mint) or {}).get("clean")}
    unknown = [mint for mint in mints if mint not in controls]
    if unknown:
        try:
            accounts = read_accounts(unknown, rpc)
        except ValueError:
            # A spent budget or a failed read: the clean answers already kept still count.
            LOG.warning("Mint control reads failed", exc_info=True)
            return controls
        for mint, account in accounts.items():
            try:
                info = account["data"]["parsed"]["info"]  # type: ignore[index]
            except (KeyError, TypeError):
                continue
            controls[mint] = classify_mint(info)
    return controls


def top10_share(mint: str, *, supply: float, exclude: set[str], rpc: Rpc) -> float | None:
    """Percent of supply held by the ten largest wallets, under the rules.

    Reads the top 20 accounts, each account's owner, and the program behind
    each program-address owner (three one-credit reads), then applies the
    rules' own count.
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
    return top10_share_from(
        accounts, owners, programs, supply=supply, program_address=is_program_address
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
                and (raw[:8] == discriminator if discriminator else len(raw) == AMM_V4_SIZE)
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
        results[pool] = {"left_pct": lp_left_pct(outstanding, issued), "dex": name}
    return results


def holder_counts(
    mints: list[str],
    cached: dict[str, Any],
    *,
    rpc: Rpc,
    at: datetime,
) -> dict[str, Any]:
    """Holders from Helius's token accounts for the mint, a few a cycle, kept six hours.

    One page of up to 1,000 accounts costs 10 credits. A holder is an owner
    with a balance; a full page is a lower bound, which is all the standard
    needs.
    """

    counts = {
        mint: entry
        for mint, entry in cached.items()
        if at - datetime.fromisoformat(entry["checked_at"]) <= HOLDERS_TTL
        and entry.get("source") == "helius"
    }
    looked = 0
    for mint in mints:
        if mint in counts or looked >= MAX_COUNT_LOOKUPS:
            continue
        looked += 1
        try:
            result = rpc(
                {
                    "jsonrpc": "2.0",
                    "id": "holders",
                    "method": "getTokenAccounts",
                    "params": {"mint": mint, "limit": HOLDER_PAGE},
                },
                credits=10,
            )["result"]
            accounts = result["token_accounts"]
            owners = {
                account["owner"] for account in accounts if int(account.get("amount") or 0) > 0
            }
        except Exception:
            LOG.warning("Holder count unavailable for %s", mint, exc_info=True)
            continue
        counts[mint] = {
            "checked_at": at.isoformat(),
            "count": len(owners),
            "at_least": len(accounts) >= HOLDER_PAGE,
            "source": "helius",
        }
    return counts


def ratify_rows(
    rows: list[dict[str, Any]],
    saved: dict[str, Any],
    *,
    vaults: dict[str, str],
    bundled: set[str],
    rpc: Rpc,
    at: datetime,
    facts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attach `ratification` to every row; read the chain only for close candidates.

    `vaults` maps a pool address to its own token account; `bundled` holds the
    mints a launch-bundle check flagged. Returns the saved state for next cycle.
    When `facts` is given, it receives each mint's inputs to the rules.
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
        and passes_free_standards(row, row["token_address"] in bundled, at)
    ]
    controls = mint_controls([row["token_address"] for row in candidates], known_controls, rpc=rpc)
    known_controls.update(controls)
    kept_locks = {
        pool: entry
        for pool, entry in (saved.get("locks") or {}).items()
        if isinstance(entry, dict) and at - datetime.fromisoformat(entry["checked_at"]) <= LOCKS_TTL
    }
    try:
        locks = liquidity_locks([row["pool_address"] for row in candidates], rpc=rpc)
        kept_locks.update(
            {pool: {**lock, "checked_at": at.isoformat()} for pool, lock in locks.items()}
        )
    except Exception:
        # A failed read keeps the last answer for an hour; a pulled pool shows on the record.
        LOG.warning("Liquidity lock reads failed", exc_info=True)
        locks = {
            pool: {key: value for key, value in entry.items() if key != "checked_at"}
            for pool, entry in kept_locks.items()
        }
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
    # The holder count is the last read: only coins passing the rest.
    finalists = [
        row["token_address"]
        for row in candidates
        if (controls.get(row["token_address"]) or {}).get("clean")
        and (holders.get(row["token_address"]) or {}).get("top10_pct") is not None
        and holders[row["token_address"]]["top10_pct"] <= MAX_TOP10_PCT
        and (locks.get(row["pool_address"]) or {}).get("left_pct") is not None
        and locks[row["pool_address"]]["left_pct"] <= MAX_LP_LEFT_PCT
    ]
    counts = holder_counts(finalists, saved.get("counts") or {}, rpc=rpc, at=at)
    for row in rows:
        mint = row.get("token_address")
        inputs = {
            "controls": known_controls.get(mint),
            "top10_pct": (holders.get(mint) or {}).get("top10_pct"),
            "bundled": mint in bundled,
            "holder_count": (counts.get(mint) or {}).get("count"),
            "holder_count_at_least": bool((counts.get(mint) or {}).get("at_least")),
            "lock": locks.get(row.get("pool_address", "")),
        }
        row["ratification"] = standards(
            row,
            inputs["controls"],
            inputs["top10_pct"],
            inputs["bundled"],
            at,
            holder_count=inputs["holder_count"],
            holder_count_at_least=inputs["holder_count_at_least"],
            lock=inputs["lock"],
        )
        if facts is not None and mint:
            facts[mint] = {"row": {key: row.get(key) for key in ROW_FACTS}, **inputs, "at": at}
    return {"controls": known_controls, "holders": holders, "counts": counts, "locks": kept_locks}
