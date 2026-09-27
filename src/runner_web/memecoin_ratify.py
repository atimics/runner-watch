"""Ratified: a memecoin that meets RATi's basic standards.

Seven standards, all required. Not an endorsement or advice: it says the
token's controls, pool, age, record and holders pass basic checks.

The mint account (one credit per 100) answers the three control standards;
a revoked authority cannot come back and Token-2022 extensions are fixed at
creation, so a clean answer is kept. Top holders cost a credit a coin and are
read only for coins that pass everything else, every six hours.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from runner_web.memecoin_chain_prices import Rpc, read_accounts
from runner_web.solana_keys import is_program_address

MIN_LIQUIDITY_USD = 10_000.0
MIN_AGE = timedelta(hours=24)
MAX_TOP10_PCT = 30.0
HOLDERS_TTL = timedelta(hours=6)
# Bump when the holder count changes, so answers under older rules are redone.
HOLDER_RULES = 3
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
}
NOTE = (
    "Ratified: meets RATi's seven basic standards for a memecoin. "
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


def top10_share(mint: str, *, supply: float, exclude: set[str], rpc: Rpc) -> float | None:
    """Percent of supply held by the ten largest wallets.

    Accounts owned by a program address (any DEX pool, bonding curve or lock)
    are left out: a program-derived owner is never a person's key. That needs
    one more read of the top 20 accounts (a credit).
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
    owners = read_accounts([item["address"] for item in accounts], rpc)
    held = []
    for item in accounts:
        try:
            owner = owners[item["address"]]["data"]["parsed"]["info"]["owner"]  # type: ignore[index]
        except (KeyError, TypeError):
            owner = None  # unknown owners count: the standard must not pass on a gap
        if owner and is_program_address(owner):
            continue
        held.append(float(item.get("uiAmount") or 0))
    held.sort(reverse=True)
    if len(held) < 10 and len(accounts) >= LARGEST_LISTED:
        # The largest wallets sit below the listed accounts, so none holds more
        # than the smallest listed: count that as an upper bound for each.
        smallest = min(float(item.get("uiAmount") or 0) for item in accounts)
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


def standards(
    row: dict[str, Any],
    controls: dict[str, Any] | None,
    top10_pct: float | None,
    bundled: bool,
    at: datetime,
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
    }
    met = sum(result is True for result in results.values())
    return {
        "ratified": all(result is True for result in results.values()),
        "met": met,
        "total": len(results),
        "standards": [
            {"key": key, "label": LABELS[key], "met": results[key], "detail": details[key]}
            for key in LABELS
        ],
        "note": NOTE,
        "as_of": at.isoformat(),
    }


def ratify_rows(
    rows: list[dict[str, Any]],
    saved: dict[str, Any],
    *,
    vaults: dict[str, str],
    bundled: set[str],
    rpc: Rpc,
    at: datetime,
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
    reads = 0
    for row in candidates:
        mint = row["token_address"]
        control = controls.get(mint)
        if not control or not control["clean"] or mint in holders or reads >= MAX_HOLDER_READS:
            continue
        reads += 1
        share = top10_share(
            mint,
            supply=control["supply"],
            exclude={vault for vault in (vaults.get(row.get("pool_address", "")),) if vault},
            rpc=rpc,
        )
        holders[mint] = {"checked_at": at.isoformat(), "top10_pct": share, "rules": HOLDER_RULES}
    for row in rows:
        mint = row.get("token_address")
        row["ratification"] = standards(
            row,
            known_controls.get(mint),
            (holders.get(mint) or {}).get("top10_pct"),
            mint in bundled,
            at,
        )
    return {"controls": known_controls, "holders": holders}
