"""Memecoin standards: RATi Rules for a memecoin.

Nine standards, all required, over facts the caller gathers from the chain and
GeckoTerminal: the token's controls, its pool, age, record, holders and pool
liquidity. Not an endorsement, a guarantee or advice.

Everything here is a rule or the arithmetic a rule needs. How and how often the
facts are read belongs to the caller; see the facts in rules.toml.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from ratitrust import rules
from ratitrust.keys import is_program_address
from ratitrust.summary import summarize

MIN_LIQUIDITY_USD = float(rules.value("memecoin", "pool", "real_liquidity"))
MIN_AGE = timedelta(hours=rules.value("memecoin", "age", "pool_age"))
MAX_TOP10_PCT = float(rules.value("memecoin", "holders", "top10_share"))
MIN_HOLDERS = int(rules.value("memecoin", "holder_count", "holder_count"))
# The pool's liquidity tokens still in existence; the rest were burned.
MAX_LP_LEFT_PCT = float(rules.value("memecoin", "liquidity_lock", "lp_tokens_left"))
# getTokenLargestAccounts lists this many accounts.
LARGEST_LISTED = int(rules.asset("memecoin")["params"]["largest_listed"])
# Token-2022 features that let someone tax, block, seize or pause holders.
RISKY_EXTENSIONS = set(rules.listed("memecoin", "risky_extensions"))
# Pools and curves hold supply for everyone, so their accounts are left out of
# the top holders. An account owned by a program address is left out only when
# that address belongs to one of these programs: anyone can hold supply under a
# program of their own, and that must count.
POOL_PROGRAMS = set(rules.listed("memecoin", "pool_programs"))
# Pool authorities with no account of their own to show a program.
POOL_AUTHORITIES = set(rules.listed("memecoin", "pool_authorities"))
RISK_KINDS = rules.listed("memecoin", "risk_kinds")
LABELS = rules.labels("memecoin")
NOTE = rules.asset("memecoin")["note"]
UNRATIFIED_NOTE = rules.asset("memecoin")["unratified_note"]
# Token accounts read for the holder count; a full page is a lower bound.
HOLDER_PAGE = int(rules.asset("memecoin")["params"]["holder_page"])


def classify_mint(info: dict[str, Any]) -> dict[str, Any]:
    """Mint and freeze authority, risky extensions and supply from a parsed mint account."""

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
    return entry


def account_amount(item: dict[str, Any]) -> float:
    """The raw amount scaled by decimals; uiAmount can be null and read as zero."""

    try:
        return int(item["amount"]) / 10 ** int(item["decimals"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Holder amount missing") from None


def top10_share_from(
    accounts: Iterable[dict[str, Any]],
    owners: dict[str, str | None],
    programs: dict[str, str | None],
    *,
    supply: float,
    exclude: set[str] = frozenset(),  # type: ignore[assignment]
    program_address: Callable[[str], bool] = is_program_address,
) -> float | None:
    """Percent of supply held by the ten largest wallets.

    `accounts` are the largest token accounts, `owners` each account's owner
    (None when unreadable) and `programs` the program behind each
    program-address owner. An account is left out only when a known pool or
    curve holds it; an unreadable owner counts. When fewer than ten wallets are
    among a full listing, each missing one counts at the smallest listed
    balance, an upper bound on anything further down.
    """

    listed = [item for item in accounts if item.get("address") not in exclude]
    if not listed or supply <= 0:
        return None
    held = []
    for item in listed:
        owner = owners.get(item["address"])
        if owner in POOL_AUTHORITIES:
            continue
        if owner and program_address(owner) and programs.get(owner) in POOL_PROGRAMS:
            continue
        held.append(account_amount(item))
    if not held and len(listed) < LARGEST_LISTED:
        return None  # every listed account is a pool: no wallet was read, not 0%
    held.sort(reverse=True)
    if len(held) < 10 and len(listed) >= LARGEST_LISTED:
        smallest = min(account_amount(item) for item in listed)
        held += [smallest] * (10 - len(held))
    return sum(held[:10]) / supply * 100


def lp_left_pct(outstanding: int, issued: int) -> float | None:
    """How much of a pool's liquidity-token supply is still out, in percent."""

    return min(100.0, outstanding / issued * 100) if issued > 0 else None


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


def real_liquidity(row: dict[str, Any]) -> float | None:
    """Twice the quote side in the pool, in dollars, when the caller could read it.

    A pool's quoted liquidity values the coin at its own price, so it is never
    used in its place.
    """

    value = row.get("real_liquidity_usd")
    return None if value is None else float(value)


def passes_free_standards(row: dict[str, Any], bundled: bool, at: datetime) -> bool:
    """Pool, age and record: known without a read, so they decide who is worth one."""

    age = _age(row, at)
    liquidity = real_liquidity(row)
    return (
        row.get("venue") == "pool"
        and liquidity is not None
        and liquidity >= MIN_LIQUIDITY_USD
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
    *,
    holder_count: int | None = None,
    holder_count_at_least: bool = False,
    lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Each standard as met, not met or not yet known, and whether all are met."""

    flagged = _flags(row, bundled)
    age = _age(row, at)
    liquidity = real_liquidity(row)
    on_curve = row.get("venue") == "bonding_curve"
    results = {
        "mint_authority": None if controls is None else not controls["mint_authority"],
        "freeze_authority": None if controls is None else not controls["freeze_authority"],
        "token_features": None if controls is None else not controls["risky_extensions"],
        "pool": False
        if row.get("venue") != "pool"
        else None
        if liquidity is None
        else liquidity >= MIN_LIQUIDITY_USD,
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
        if on_curve
        else f"${liquidity:,.0f} real liquidity"
        if liquidity is not None
        else "quote side not read",
        "age": f"{age.total_seconds() / 3600:.0f} hours" if age is not None else "",
        "record": ", ".join(flagged),
        "holders": f"top 10 hold {top10_pct:.0f}%" if top10_pct is not None else "",
        "holder_count": f"{holder_count:,}{'+' if holder_count_at_least else ''} holders"
        if holder_count is not None
        else "",
        "liquidity_lock": _lock_detail(lock),
    }
    return summarize(
        LABELS,
        results,
        details,
        note=NOTE,
        unratified_note=UNRATIFIED_NOTE,
        as_of=at.isoformat(),
    )


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
