from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_watch.ingestion import SourceFetch
from runner_watch.xml_security import read_limited
from runner_web.db import connection
from runner_web.helius_discovery import RPC_URL, Rpc, rpc_request
from runner_web.ingestion import record_source_fetch
from runner_web.memecoin_chain_ingestion import collect_chain as discover_pools
from runner_web.memecoin_chain_parser import coin_search_rank, short_address
from runner_web.memecoin_chain_prices import chain_prices
from runner_web.memecoin_copycats import copycat_bursts, find_originals, mark_copycats
from runner_web.memecoin_early import early_signal
from runner_web.memecoin_forensics import analyze_events
from runner_web.memecoin_integrity import creator_trades
from runner_web.memecoin_liquidity import attach_real_liquidity
from runner_web.memecoin_model import assess_memecoin, display_assessment
from runner_web.memecoin_ratify import ratify_rows
from runner_web.memecoin_ratify import standards as ratify_standards
from runner_web.memecoin_store import (
    memecoin_history,
    memecoin_state_changes,
    save_memecoin_snapshot,
    stored_memecoin,
)
from runner_web.memecoin_watch import creator_sells, launch_bundles, recent_findings
from runner_web.ratification_records import safely_record

LOG = logging.getLogger(__name__)
POOL_QUOTES_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/multi/"
SOURCE = "GeckoTerminal"
MIN_POOL_LIQUIDITY_USD = 1000
# Below this a single trade moves the price a long way, so a Call would settle
# at a quote nobody could trade at.
MIN_CALL_LIQUIDITY_USD = 5000
COLLAPSE_CHANGE_PCT = -90.0
REFRESH_SECONDS = 300
STALE_SECONDS = 900
# Bonding curves quoted each cycle: those that traded last cycle keep their
# place, and the newest launches fill the rest.
CURVE_SLOTS = 90
# Graduated pools that traded last cycle keep their place the same way; with the
# graduation stream, the newest alone would turn the list over every few hours.
ACTIVE_POOL_SLOTS = 60
POOL_SLOTS = 100
# Originals of copied launch names, found by name search and checked by address.
ORIGINAL_SLOTS = 10
# Coins people searched for by address that we were not tracking.
SEARCHED_SLOTS = 20
SEARCHED_DAYS = 7
SEARCH_QUEUE_LOCK_ID = 728416204
SEARCH_MISSES = 3
SEARCH_DEAD_HOURS = 24
SEARCH_CHECKED_KEPT = 500
# Graduated pools, bonding curves, originals and searched coins are quoted in one pass.
MAX_QUOTED_POOLS = POOL_SLOTS + CURVE_SLOTS + ORIGINAL_SLOTS + SEARCHED_SLOTS
SOLANA_ADDRESS = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
# GeckoTerminal answers a burst of batch requests with 429, so space them out.
# Two seconds still drew a 429 on the fourth of seven batches in production.
QUOTE_PAUSE_SECONDS = 5.0
# A rate-limited pool batch waits this long and tries once more.
RATE_LIMIT_RETRY_SECONDS = 30.0
# Nobody has read a memecoin page for this long: skip the paid sampling pages.
QUIET_AFTER_SECONDS = 1800
# Each web process records a view at most this often.
VIEW_NOTE_SECONDS = 60
# With chain prices, GeckoTerminal brings activity windows for this many coins.
ACTIVITY_SHORTLIST = 30
# A bonding curve is shown once someone has put this much into it.
MIN_CURVE_LIQUIDITY_USD = 100.0
ACTIVE_CURVE_SLOTS = 60
CURVE_HOURS = 24
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
Download = Callable[[str, float], bytes]


def memecoins_enabled() -> bool:
    return os.getenv("MEMECOINS_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def _number(value: Any, *, minimum: float | None = None) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        return None
    return number


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def normalize_memecoins(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload or len(payload) > 100:
        raise ValueError("Expected up to 100 memecoin market rows")
    rows: dict[str, dict[str, Any]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        coin_id = str(item.get("id") or "")
        symbol = str(item.get("symbol") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        price = _number(item.get("current_price"), minimum=0)
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", coin_id)
            or not symbol
            or len(symbol) > 32
            or not name
            or len(name) > 160
            or price is None
            or price <= 0
        ):
            continue
        observed_at = _time(item.get("last_updated"))
        rows.setdefault(
            coin_id,
            {
                "id": coin_id,
                "symbol": symbol,
                "name": name,
                "price": price,
                "change_24h": _number(item.get("price_change_percentage_24h"), minimum=-100),
                "volume_24h": _number(item.get("total_volume"), minimum=0),
                "market_cap": _number(item.get("market_cap"), minimum=0),
                "observed_at": observed_at.isoformat() if observed_at else None,
                "source_url": f"https://www.coingecko.com/en/coins/{coin_id}",
                "detail_url": f"/memecoins/coin/{coin_id}",
                **{
                    field: _number(item.get(field), minimum=0)
                    for field in (
                        "high_24h",
                        "low_24h",
                        "fully_diluted_valuation",
                        "circulating_supply",
                        "total_supply",
                        "max_supply",
                    )
                },
            },
        )
    if not rows:
        raise ValueError("Memecoin feed needs valid coin IDs and positive prices")
    return list(rows.values())


def _short_windows(attrs: dict[str, Any]) -> dict[str, float | None]:
    """The pool's recent windows: the pace of a move shows here before the 24h totals."""

    changes = attrs.get("price_change_percentage") or {}
    volumes = attrs.get("volume_usd") or {}
    trades = attrs.get("transactions") or {}
    fields: dict[str, float | None] = {}
    for window in ("m5", "h1", "h6"):
        activity = trades.get(window) or {}
        fields[f"change_{window}"] = _number(changes.get(window), minimum=-100)
        fields[f"volume_{window}"] = _number(volumes.get(window), minimum=0)
        fields[f"buyers_{window}"] = _number(activity.get("buyers"), minimum=0)
        fields[f"sellers_{window}"] = _number(activity.get("sellers"), minimum=0)
    return fields


def normalize_chain_pools(payload: Any, *, at: datetime) -> list[dict[str, Any]]:
    """Discover from pool records. Token identity and selection use chain fields only."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Expected DEX pool records")
    if len(payload["data"]) > MAX_QUOTED_POOLS:
        raise ValueError(f"Expected up to {MAX_QUOTED_POOLS} pool records")
    rows: dict[str, dict[str, Any]] = {}
    for pool in payload["data"]:
        try:
            attrs = pool["attributes"]
            links = pool["relationships"]
            network = links["network"]["data"]["id"]
            token_id = links["base_token"]["data"]["id"]
            address = attrs["address"]
            if not re.fullmatch(r"[a-z0-9-]{1,40}", network):
                continue
            if not token_id.startswith(network + "_"):
                continue
            token = token_id[len(network) + 1 :]
            if not all(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", x) for x in (token, address)):
                continue
            # EVM addresses are case insensitive; Solana addresses preserve case.
            if re.fullmatch(r"0x[0-9a-fA-F]{40}", token):
                token = token.lower()
            price = _number(attrs.get("base_token_price_usd"), minimum=0)
            liquidity = _number(attrs.get("reserve_in_usd"), minimum=0)
            volume = _number(attrs.get("volume_usd", {}).get("h24"), minimum=0)
            activity = attrs.get("transactions", {}).get("h24", {})
            buys = _number(activity.get("buys"), minimum=0)
            sells = _number(activity.get("sells"), minimum=0)
            created = _time(attrs.get("pool_created_at"))
            if (
                price is None
                or price <= 0
                or liquidity is None
                or liquidity < MIN_POOL_LIQUIDITY_USD
                or volume is None
                or volume <= 0
                or buys is None
                or sells is None
                or buys + sells <= 0
                or created is None
                or created > at + timedelta(seconds=60)
            ):
                continue
            coin_id = "chain-" + hashlib.sha256(f"{network}:{token}".encode()).hexdigest()
            row = {
                "id": coin_id,
                # The contract address is the identity. Creator-chosen launch text
                # stays in claimed_* fields and never stands in for it.
                "symbol": short_address(token),
                "name": token,
                "claimed_symbol": None,
                "claimed_name": None,
                "network": network,
                "token_address": token,
                "pool_address": address,
                "pool_created_at": created.isoformat(),
                "liquidity_usd": liquidity,
                "buys_24h": buys,
                "sells_24h": sells,
                "price": price,
                "volume_24h": volume,
                "market_cap": None,
                "change_24h": _number(
                    attrs.get("price_change_percentage", {}).get("h24"), minimum=-100
                ),
                "observed_at": at.isoformat(),
                "time_basis": "indexer_fetch",
                "source": SOURCE,
                "source_url": f"https://www.geckoterminal.com/{network}/pools/{address}",
                "detail_url": f"/memecoins/coin/{coin_id}",
                "fully_diluted_valuation": _number(attrs.get("fdv_usd"), minimum=0),
                **_short_windows(attrs),
                "high_24h": None,
                "low_24h": None,
                "circulating_supply": None,
                "total_supply": None,
                "max_supply": None,
            }
            previous = rows.get(coin_id)
            # One representative pool per token; keep its price and volume together.
            if previous is None or (liquidity, volume, address) > (
                previous["liquidity_usd"],
                previous["volume_24h"],
                previous["pool_address"],
            ):
                rows[coin_id] = row
        except (KeyError, TypeError, AttributeError):
            continue
    return sorted(rows.values(), key=lambda row: (-row["volume_24h"], row["id"]))


def _download(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "RATi/1.0 https://runners.rati.chat",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return read_limited(response, max_bytes=MAX_RESPONSE_BYTES)


def _save_state(key: str, value: Any, at: datetime) -> None:
    with connection() as database:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at "
            "WHERE worker_state.updated_at<=excluded.updated_at",
            (key, json.dumps(value, allow_nan=False), at.isoformat()),
        )


def _state_dict(database: Any, key: str) -> dict[str, Any]:
    saved = database.execute("SELECT value FROM worker_state WHERE key=?", (key,)).fetchone()
    try:
        value = json.loads(saved["value"]) if saved else {}
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(database: Any, key: str, value: Any, at: datetime) -> None:
    database.execute(
        "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at "
        "WHERE worker_state.updated_at<=excluded.updated_at",
        (key, json.dumps(value, allow_nan=False), at.isoformat()),
    )


def _search_queue(database: Any) -> dict[str, str]:
    """The searched addresses and when each was asked for; empty if unreadable."""

    return _state_dict(database, "memecoin_searched")


def _lock_search_state(database: Any) -> None:
    if database.backend == "postgres":
        database.execute("SELECT pg_advisory_xact_lock(?)", (SEARCH_QUEUE_LOCK_ID,))


def _is_dead(checked: Any, current: datetime) -> bool:
    """An address the worker looked up several times and never found a pool for."""

    if not isinstance(checked, dict) or int(checked.get("misses") or 0) < SEARCH_MISSES:
        return False
    when = _time(checked.get("at"))
    return when is not None and (current - when).total_seconds() < SEARCH_DEAD_HOURS * 3600


def request_memecoin(address: str, *, at: datetime | None = None) -> bool:
    """Queue an address someone searched for; the worker quotes it next cycle.

    Only the address is kept, so the web request makes no outside call. The
    newest requests keep their place when the queue is full. An address the
    worker has already failed to find is not queued again for a day, and asking
    twice does not renew an address's place.
    """

    address = address.strip()
    if not SOLANA_ADDRESS.fullmatch(address):
        return False
    current = at or datetime.now(UTC)
    with connection() as database:
        # Read and write in one transaction, one request at a time, so two
        # searches at the same moment cannot drop each other's address.
        _lock_search_state(database)
        checked = _state_dict(database, "memecoin_checked")
        if _is_dead(checked.get(address), current):
            return False
        queue = _search_queue(database)
        if address in queue:
            return True
        checked.pop(address, None)
        queue[address] = current.isoformat()
        newest = sorted(queue.items(), key=lambda item: item[1], reverse=True)[:SEARCHED_SLOTS]
        _write_state(database, "memecoin_searched", dict(newest), current)
        _write_state(database, "memecoin_checked", checked, current)
    return True


def _record_search_results(missed: set[str], found: set[str], *, at: datetime) -> None:
    """Count a miss for each address with no pool, and drop the ones given up on.

    A dead address leaves the queue and is remembered, so it stops using a slot
    and a lookup. One that trades is forgotten from the record.
    """

    with connection() as database:
        _lock_search_state(database)
        checked = _state_dict(database, "memecoin_checked")
        queue = _search_queue(database)
        for address in found:
            checked.pop(address, None)
        for address in missed:
            entry = checked.get(address) if isinstance(checked.get(address), dict) else {}
            checked[address] = {"misses": int(entry.get("misses") or 0) + 1, "at": at.isoformat()}
            if checked[address]["misses"] >= SEARCH_MISSES:
                queue.pop(address, None)
        recent = sorted(checked.items(), key=lambda item: str(item[1].get("at")), reverse=True)
        _write_state(database, "memecoin_searched", queue, at)
        _write_state(database, "memecoin_checked", dict(recent[:SEARCH_CHECKED_KEPT]), at)


def _searched_pools(
    tracked: set[str], *, download: Download, at: datetime
) -> tuple[list[dict[str, Any]], bool]:
    """The busiest pool of each searched address, in one lookup; empty on failure."""

    with connection() as database:
        queue = _search_queue(database)
    wanted = {}
    for address, requested in queue.items():
        when = _time(requested)
        if (
            when is not None
            and 0 <= (at - when).total_seconds() <= SEARCHED_DAYS * 86400
            and SOLANA_ADDRESS.fullmatch(address)
            and address not in tracked
        ):
            wanted[address] = when
    if not wanted:
        return [], False
    addresses = list(wanted)[:SEARCHED_SLOTS]
    url = (
        "https://api.geckoterminal.com/api/v2/networks/solana/tokens/multi/"
        + ",".join(addresses)
        + "?include=top_pools"
    )
    try:
        body = json.loads(download(url, 10.0))
        pools = {
            item["id"]: float(item["attributes"].get("reserve_in_usd") or 0)
            for item in body.get("included") or []
            if item.get("type") == "pool"
        }
        found = []
        for token in body.get("data") or []:
            mint = token["id"][len("solana_") :]
            if mint not in wanted:
                continue
            choices = [
                pool["id"]
                for pool in token["relationships"]["top_pools"]["data"]
                if pool["id"] in pools
            ]
            if not choices:
                continue
            best = max(choices, key=lambda pool_id: (pools[pool_id], pool_id))
            found.append(
                {
                    "pool_address": best[len("solana_") :],
                    "token_address": mint,
                    "network": "solana",
                    "created_at": wanted[mint].isoformat(),
                    "source_url": f"https://www.geckoterminal.com/solana/pools/{best[7:]}",
                    "venue": "pool",
                    "found_by": "address_search",
                }
            )
    except Exception:
        LOG.warning("Searched coin lookup failed", exc_info=True)
        return [], True
    hits = {item["token_address"] for item in found}
    try:
        _record_search_results(set(addresses) - hits, hits, at=at)
    except Exception:
        LOG.warning("Searched coin results were not recorded", exc_info=True)
    return found, True


def _watch(
    newest: list[dict[str, Any]],
    active: list[dict[str, Any]],
    *,
    slots: int,
    active_slots: int,
    is_open: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    """What traded last cycle keeps its place, busiest first; the newest fill the rest."""

    chosen: dict[str, dict[str, Any]] = {}
    for item in active:
        if len(chosen) < active_slots and is_open(item):
            chosen.setdefault(item["pool_address"], item)
    for item in sorted(newest, key=lambda item: item["created_at"], reverse=True):
        if len(chosen) >= slots:
            break
        if is_open(item):
            chosen.setdefault(item["pool_address"], item)
    return list(chosen.values())


def _curve_watch(
    launches: list[dict[str, Any]],
    active: list[dict[str, Any]],
    graduated: set[str],
    at: datetime,
) -> list[dict[str, Any]]:
    """Which launches' bonding curves to quote this cycle."""

    def open_curve(curve: dict[str, Any]) -> bool:
        created = _time(curve.get("created_at"))
        return (
            created is not None
            and 0 <= (at - created).total_seconds() <= CURVE_HOURS * 3600
            and curve.get("token_address") not in graduated
        )

    return _watch(
        launches, active, slots=CURVE_SLOTS, active_slots=ACTIVE_CURVE_SLOTS, is_open=open_curve
    )


def _saved_list(key: str) -> list[dict[str, Any]]:
    with connection() as database:
        saved = database.execute("SELECT value FROM worker_state WHERE key=?", (key,)).fetchone()
    try:
        value = json.loads(saved["value"]) if saved else []
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


# monotonic() can be under a minute on a fresh machine, so the first view always counts.
_view_noted_at = float("-inf")


def is_memecoin_view(path: str) -> bool:
    """A person reading memecoins: the pages, the board API and a coin's live refresh.

    Share cards and replay GIFs are left out: Telegram fetches them when our own
    bot posts, which would keep the worker awake with nobody reading.
    """

    if path.endswith((".png", ".gif", ".json")) or "/replays/" in path:
        return False
    return (
        path == "/memecoins"
        or path.startswith("/memecoins/coin/")
        or path == "/api/memecoins"
        or path.startswith("/api/screens/memecoins/")
    )


def view_note_due(now: float) -> bool:
    """Whether this process should record a view now (once a minute at most)."""

    global _view_noted_at
    if now - _view_noted_at < VIEW_NOTE_SECONDS:
        return False
    _view_noted_at = now
    return True


def note_memecoin_view(at: datetime | None = None) -> None:
    current = at or datetime.now(UTC)
    _save_state("memecoin_last_view", current.isoformat(), current)


def memecoins_quiet(at: datetime) -> bool:
    """True when nobody has read a memecoin page for half an hour.

    With no view recorded yet (a new install) the worker samples in full.
    """

    seen = _time(_market_states(keys=("memecoin_last_view",)).get("memecoin_last_view"))
    return seen is not None and (at - seen).total_seconds() > QUIET_AFTER_SECONDS


def _watch_findings(rows: list[dict[str, Any]], *, rpc: Rpc, at: datetime) -> list[dict[str, Any]]:
    """This cycle's creator-sell and launch-bundle findings plus a day of earlier ones.

    Each new finding records the coin's tag just before it, so alerts go to
    coins people were watching. A failed check skips; it never fails the refresh.
    """

    saved = _market_states(
        keys=(
            "memecoins_snapshot",
            "memecoin_creator_balances",
            "memecoin_launch_checks",
            "memecoin_watch_findings",
        )
    )
    before = {
        row.get("token_address"): (row.get("early") or {}).get("state")
        for row in (saved.get("memecoins_snapshot") or {}).get("rows") or []
        if isinstance(row, dict)
    }
    new: list[dict[str, Any]] = []
    try:
        sells, balances = creator_sells(
            rows, saved.get("memecoin_creator_balances") or {}, rpc=rpc, at=at
        )
        _save_state("memecoin_creator_balances", balances, at)
        new += sells
    except Exception:
        LOG.warning("Creator balance check failed", exc_info=True)
    try:
        ranked = [{**row, "early": {"state": before.get(row["token_address"])}} for row in rows]
        bundles, checked = launch_bundles(
            ranked, saved.get("memecoin_launch_checks") or {}, rpc=rpc, at=at
        )
        _save_state("memecoin_launch_checks", checked, at)
        new += bundles
    except Exception:
        LOG.warning("Launch bundle checks failed", exc_info=True)
    for finding in new:
        finding["state_before"] = before.get(finding["token_address"])
    findings = recent_findings(new + list(saved.get("memecoin_watch_findings") or []), at)
    _save_state("memecoin_watch_findings", findings, at)
    return findings


def _ratify(
    rows: list[dict[str, Any]],
    chain: dict[str, Any] | None,
    *,
    rpc: Rpc,
    at: datetime,
) -> None:
    """Attach each row's ratification; a failed read leaves standards unknown."""

    saved = _market_states(keys=("memecoin_ratification", "memecoin_launch_checks"))
    # A bundled launch stays on the record for as long as the check is remembered.
    bundled = {
        mint
        for mint, entry in (saved.get("memecoin_launch_checks") or {}).items()
        if isinstance(entry, dict) and entry.get("finding")
    }
    vaults = {
        address: quote["base_vault"]
        for address, quote in ((chain or {}).get("prices") or {}).items()
        if quote.get("base_vault")
    }
    inputs: dict[str, dict[str, Any]] = {}
    try:
        state = ratify_rows(
            rows,
            saved.get("memecoin_ratification") or {},
            vaults=vaults,
            bundled=bundled,
            rpc=rpc,
            at=at,
            facts=inputs,
        )
        _save_state("memecoin_ratification", state, at)
        safely_record(
            "memecoin",
            {
                mint: (row["ratification"], inputs[mint])
                for row in rows
                if (mint := row.get("token_address")) in inputs
            },
            at=at,
        )
    except Exception:
        LOG.warning("Ratification reads failed", exc_info=True)
        for row in rows:
            row.setdefault(
                "ratification",
                ratify_standards(row, None, None, row.get("token_address") in bundled, at),
            )


def price_source() -> str:
    """Where board prices come from: gecko, shadow (gecko, with chain compared) or chain."""

    value = os.getenv("MEMECOIN_PRICE_SOURCE", "shadow").strip().lower()
    return value if value in {"gecko", "shadow", "chain"} else "shadow"


def _gecko_quotes(
    addresses: list[str],
    allowed: dict[str, dict[str, Any]],
    extra: set[str],
    *,
    download: Download,
    pause_first: bool,
) -> list[dict[str, Any]]:
    """GeckoTerminal pool quotes in batches of 30, spaced against its rate limit."""

    payload = []
    for offset in range(0, len(addresses), 30):
        chunk = addresses[offset : offset + 30]
        if offset or pause_first:
            time.sleep(QUOTE_PAUSE_SECONDS)
        url = "https://api.geckoterminal.com/api/v2/networks/solana/pools/multi/" + ",".join(chunk)
        try:
            body = download(url, 10.0)
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            if extra.issuperset(chunk):
                LOG.warning("Pool quotes rate limited; skipped %d extras", len(addresses) - offset)
                break
            # The pools are the board itself, so wait out the limit once before failing.
            LOG.warning("Pool quotes rate limited; retrying in %ds", RATE_LIMIT_RETRY_SECONDS)
            time.sleep(RATE_LIMIT_RETRY_SECONDS)
            body = download(url, 10.0)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Pool quote response exceeds the size limit")
        batch = json.loads(body)
        if not isinstance(batch, dict) or not isinstance(batch.get("data"), list):
            raise ValueError("Pool quote response is invalid")
        if len(batch["data"]) > 30:
            raise ValueError("Pool quote response exceeds the row limit")
        for pool in batch["data"]:
            try:
                address = pool["attributes"]["address"]
                receipt = allowed.get(address)
                if receipt is None or pool["relationships"]["base_token"]["data"]["id"] != (
                    "solana_" + receipt["token_address"]
                ):
                    continue
                pool["relationships"]["network"] = {"data": {"id": "solana"}}
                payload.append(pool)
            except (KeyError, TypeError):
                continue
    return payload


def _previous_prices() -> dict[str, float]:
    """Last cycle's price by pool address."""

    snapshot = _market_states(keys=("memecoins_snapshot",)).get("memecoins_snapshot") or {}
    return {
        str(row.get("pool_address")): float(row["price"])
        for row in snapshot.get("rows") or []
        if isinstance(row, dict) and row.get("pool_address") and _number(row.get("price"))
    }


def _activity_shortlist(addresses: list[str], priced: dict[str, dict[str, Any]]) -> list[str]:
    """The chain-priced pools most worth GeckoTerminal's activity windows.

    Half are the biggest movers since last cycle; half are pools we have no
    price for yet, since new graduations and curves are where a run starts.
    """

    previous = _previous_prices()
    moves, new = [], []
    for address in addresses:
        price = priced[address]["price"]
        before = previous.get(address)
        if before:
            moves.append((abs(price / before - 1), address))
        else:
            new.append((priced[address]["liquidity_usd"], address))
    half = ACTIVITY_SHORTLIST // 2
    chosen = [address for _, address in sorted(new, reverse=True)[:half]]
    for _, address in sorted(moves, reverse=True):
        if len(chosen) >= ACTIVITY_SHORTLIST:
            break
        chosen.append(address)
    return chosen


def _price_check(rows: list[dict[str, Any]], chain: dict[str, Any], at: datetime) -> dict[str, Any]:
    """How far chain prices sit from GeckoTerminal's, for the shadow run."""

    gaps = sorted(
        (abs(quote["price"] / row["price"] - 1) * 100, row["symbol"])
        for row in rows
        if (quote := chain["prices"].get(row["pool_address"])) and row.get("price")
    )
    return {
        "checked_at": at.isoformat(),
        "sol_usd": round(chain["sol_usd"], 4),
        "chain_priced": len(chain["prices"]),
        "compared": len(gaps),
        "median_gap_pct": round(gaps[len(gaps) // 2][0], 2) if gaps else None,
        "p90_gap_pct": round(gaps[int(len(gaps) * 0.9)][0], 2) if gaps else None,
        "worst": [{"symbol": symbol, "gap_pct": round(gap, 2)} for gap, symbol in gaps[-5:]],
    }


def _history_changes(coin_ids: list[str], at: datetime) -> dict[str, dict[str, float]]:
    """Our own saved price 1, 6 and 24 hours ago, for coins GeckoTerminal did not window."""

    found: dict[str, dict[str, float]] = {}
    if not coin_ids:
        return found
    marks = ",".join("?" * len(coin_ids))
    with connection() as database:
        for label, hours in (("h1", 1), ("h6", 6), ("24h", 24)):
            target = at - timedelta(hours=hours)
            for row in database.execute(
                f"SELECT coin_id,observed_at,price FROM memecoin_quote_history "
                f"WHERE coin_id IN ({marks}) AND observed_at>=? AND observed_at<=? "
                "ORDER BY observed_at",
                (
                    *coin_ids,
                    (target - timedelta(minutes=10)).isoformat(),
                    (target + timedelta(minutes=10)).isoformat(),
                ),
            ).fetchall():
                found.setdefault(row["coin_id"], {})[label] = float(row["price"])
    return found


def _chain_row(receipt: dict[str, Any], quote: dict[str, Any], at: datetime) -> dict[str, Any]:
    """A board row priced from the chain, before any activity windows are known."""

    token, address = receipt["token_address"], receipt["pool_address"]
    coin_id = "chain-" + hashlib.sha256(f"solana:{token}".encode()).hexdigest()
    return {
        "id": coin_id,
        "symbol": short_address(token),
        "name": token,
        "claimed_symbol": None,
        "claimed_name": None,
        "network": "solana",
        "token_address": token,
        "pool_address": address,
        "pool_created_at": receipt.get("created_at"),
        "liquidity_usd": quote["liquidity_usd"],
        "buys_24h": None,
        "sells_24h": None,
        "price": quote["price"],
        "volume_24h": None,
        "market_cap": None,
        "change_24h": None,
        "observed_at": at.isoformat(),
        "time_basis": "chain_read",
        "source": "Solana (Helius)",
        "source_url": f"https://solscan.io/account/{address}",
        "detail_url": f"/memecoins/coin/{coin_id}",
        "fully_diluted_valuation": None,
        **{
            f"{field}_{window}": None
            for field in ("change", "volume", "buyers", "sellers")
            for window in ("m5", "h1", "h6")
        },
        "high_24h": None,
        "low_24h": None,
        "circulating_supply": None,
        "total_supply": None,
        "max_supply": None,
    }


def _with_chain_prices(
    rows: list[dict[str, Any]],
    allowed: dict[str, dict[str, Any]],
    chain: dict[str, Any],
    at: datetime,
) -> list[dict[str, Any]]:
    """Chain price and liquidity for every pool it read; GeckoTerminal keeps the rest.

    A shortlisted row keeps GeckoTerminal's activity windows under the chain
    price. Price changes GeckoTerminal did not give come from our saved history.
    """

    priced = chain["prices"]
    by_pool = {row["pool_address"]: row for row in rows}
    for address, quote in priced.items():
        receipt = allowed[address]
        on_curve = receipt.get("venue") == "bonding_curve"
        if not on_curve and quote["liquidity_usd"] < MIN_POOL_LIQUIDITY_USD:
            continue
        if on_curve and quote["liquidity_usd"] < MIN_CURVE_LIQUIDITY_USD:
            continue  # nobody has bought into it yet
        row = by_pool.get(address)
        if row is None:
            by_pool[address] = _chain_row(receipt, quote, at)
        else:
            row.update(
                price=quote["price"],
                liquidity_usd=quote["liquidity_usd"],
                source="Solana (Helius)",
                activity_source=row["source"],
            )
    merged = list(by_pool.values())
    history = _history_changes([row["id"] for row in merged if row.get("change_h1") is None], at)
    for row in merged:
        for label, key in (("h1", "change_h1"), ("h6", "change_h6"), ("24h", "change_24h")):
            before = history.get(row["id"], {}).get(label)
            if row.get(key) is None and before:
                row[key] = round((row["price"] / before - 1) * 100, 3)
    return sorted(merged, key=lambda row: (-(row.get("volume_24h") or 0), row["id"]))


def _collect_helius(
    *, download: Download, at: datetime, rpc: Rpc | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    quiet = memecoins_quiet(at)
    discovery = discover_pools(at=at, rpc=rpc, quiet=quiet)
    with connection() as database:
        saved = database.execute(
            "SELECT value FROM worker_state WHERE key='helius_discovered_pools'"
        ).fetchone()
    try:
        previous = json.loads(saved["value"]) if saved else []
    except (ValueError, TypeError):
        previous = []

    def recent_pool(pool: dict[str, Any]) -> bool:
        created = _time(pool.get("created_at"))
        return bool(created) and 0 <= (at - created).total_seconds() <= 30 * 86400

    selected = _watch(
        discovery["pools"] + previous,
        _saved_list("memecoin_pool_watch"),
        slots=POOL_SLOTS,
        active_slots=ACTIVE_POOL_SLOTS,
        is_open=recent_pool,
    )
    # Keep chain discoveries while USD quotes become available in the pool index.
    _save_state("helius_discovered_pools", selected, at)
    analytics = analyze_events(discovery.get("events", []))
    _save_state("memecoin_forensics", analytics, at)
    alerts = creator_trades(discovery.get("transactions", []), selected, at=at)
    with connection() as database:
        saved_alerts = database.execute(
            "SELECT value FROM worker_state WHERE key='memecoin_integrity_alerts'"
        ).fetchone()
    previous_alerts = json.loads(saved_alerts["value"]) if saved_alerts else []
    unique_alerts = {}
    for alert in alerts + previous_alerts:
        observed = _time(alert.get("observed_at"))
        if observed and 0 <= (at - observed).total_seconds() <= 86400:
            unique_alerts.setdefault(alert["id"], alert)
    ledger = sorted(unique_alerts.values(), key=lambda item: item["observed_at"], reverse=True)[
        :1000
    ]
    _save_state("memecoin_integrity_alerts", ledger, at)
    coverage = {
        "checked_at": at.isoformat(),
        "program": "Pump, PumpSwap, Raydium CPMM",
        "streams": discovery.get("coverage", {}).get("streams", []),
        "budget": discovery.get("coverage", {}).get("budget", {}),
        "recorded_coverage_gaps": discovery.get("coverage", {}).get("recorded_coverage_gaps", 0),
        "commitment": "finalized",
        "received_transactions": discovery.get("received_transactions", 0),
        "partial": discovery.get("partial", False),
        "mode": "resumable",
    }
    _save_state("memecoin_integrity_coverage", coverage, at)
    curves = _curve_watch(
        discovery.get("curves", []),
        _saved_list("memecoin_curve_watch"),
        {item["token_address"] for item in selected},
        at,
    )
    bursts = copycat_bursts(discovery.get("events", []), at)
    with connection() as database:
        saved_originals = database.execute(
            "SELECT value FROM worker_state WHERE key='memecoin_copycat_originals'"
        ).fetchone()
    try:
        original_cache = json.loads(saved_originals["value"]) if saved_originals else {}
    except (ValueError, TypeError):
        original_cache = {}
    original_cache = original_cache if isinstance(original_cache, dict) else {}
    # Quote the originals already found; new searches wait until the pools are in.
    originals, _, _ = find_originals(
        bursts, original_cache, download=download, at=at, pause=0, max_searches=0
    )
    tracked = {item["token_address"] for item in selected + curves}
    searched, looked_up = _searched_pools(tracked, download=download, at=at)
    tracked |= {item["token_address"] for item in searched}
    found = [
        {
            "pool_address": original["pool_address"],
            "token_address": original["token_address"],
            "network": "solana",
            "created_at": original["created_at"],
            "source_url": f"https://www.geckoterminal.com/solana/pools/{original['pool_address']}",
            "venue": "pool",
            "found_by": "copycat_name_search",
        }
        for original in list(originals.values())[:ORIGINAL_SLOTS]
        if original["token_address"] not in tracked
    ]
    # Graduated pools first, then the few originals, then curves, busiest first:
    # a rate limit cuts the end of the list.
    allowed = {item["pool_address"]: item for item in selected + found + searched + curves}
    extra = {item["pool_address"] for item in curves + found + searched}
    source = price_source()
    chain = None
    if source in ("chain", "shadow"):
        try:
            chain = chain_prices(
                [item for item in allowed.values() if item.get("venue") != "bonding_curve"],
                curves,
                rpc=rpc or rpc_request,
            )
        except Exception:
            LOG.warning("Chain prices failed; quoting from GeckoTerminal", exc_info=True)
    addresses = list(allowed)
    if source == "chain" and chain is not None:
        priced = chain["prices"]
        # GeckoTerminal now only prices what the chain reader cannot and brings
        # activity for a shortlist; all of it may be cut by a rate limit except
        # the pools only it can price.
        shortlist = _activity_shortlist(
            [address for address in addresses if address in priced], priced
        )
        addresses = [address for address in addresses if address not in priced] + shortlist
        extra |= set(shortlist)
    payload = _gecko_quotes(addresses, allowed, extra, download=download, pause_first=looked_up)
    rows = normalize_chain_pools({"data": payload}, at=at)
    if chain is not None and source == "shadow":
        _save_state("memecoin_price_check", _price_check(rows, chain, at), at)
    if chain is not None and source == "chain":
        rows = _with_chain_prices(rows, allowed, chain, at)
    # Search for new originals last, so a rate limit here costs only the search;
    # what is found is quoted from the next cycle.
    _, original_cache, _ = find_originals(
        bursts, original_cache, download=download, at=at, pause=QUOTE_PAUSE_SECONDS
    )
    _save_state("memecoin_copycat_originals", original_cache, at)
    # Creator selling and bundled launches, from one-credit reads; they join the
    # forensic findings so the assessment sets AVOID and shows their receipts.
    analytics["findings"] = analytics["findings"] + _watch_findings(
        rows, rpc=rpc or rpc_request, at=at
    )
    _save_state("memecoin_forensics", analytics, at)
    claims = {
        event["token_address"]: event
        for event in sorted(discovery.get("events", []), key=lambda event: event["observed_at"])
        if event.get("kind") == "token_launch"
    }
    for row in rows:
        row["discovery"] = allowed[row["pool_address"]]
        row["discovery_source"] = {
            "copycat_name_search": "GeckoTerminal name search",
            "address_search": "Searched by address",
        }.get(row["discovery"].get("found_by"), "Helius")
        row["venue"] = row["discovery"].get("venue", "pool")
        launch = claims.get(row["token_address"]) or {}
        row["claimed_symbol"] = launch.get("claimed_symbol") or None
        row["claimed_name"] = launch.get("claimed_name") or None
        row.update(
            assess_memecoin(
                row,
                events=discovery.get("events", []),
                findings=analytics["findings"],
                coverage=coverage,
                at=at,
            )
        )
    mark_copycats(rows, bursts, originals)
    for row in rows:
        row["early"] = early_signal(row)
    attach_real_liquidity(rows, download=download)
    _ratify(rows, chain, rpc=rpc or rpc_request, at=at)
    # A pool or curve that traded keeps its slot next cycle, busiest first.
    # Without GeckoTerminal's windows, a chain price that moved since last
    # cycle is the sign of trading.
    previous = _previous_prices()

    def activity(row: dict[str, Any]) -> tuple[float, float]:
        before = previous.get(row["pool_address"])
        move = abs(row["price"] / before - 1) if before else 0.0
        return (row.get("volume_h1") or 0.0, move)

    traded = sorted(rows, key=activity, reverse=True)
    for key, venue in (("memecoin_curve_watch", "bonding_curve"), ("memecoin_pool_watch", "pool")):
        _save_state(
            key,
            [
                row["discovery"]
                for row in traded
                if row["venue"] == venue
                and not row["discovery"].get("found_by")
                and (activity(row)[0] > 0 or activity(row)[1] > 0.001)
            ],
            at,
        )
    metadata = {
        key: value
        for key, value in discovery.items()
        if key not in {"pools", "curves", "transactions", "events"}
    }
    metadata.update(
        discovered_pools=len(discovery["pools"]),
        tracked_pools=len(selected),
        tracked_curves=len(curves),
        quiet=quiet,
        quoted_pools=len(rows),
        selection="helius_pumpswap_create_pool",
    )
    return rows, metadata


def refresh_memecoins(
    *, download: Download | None = None, at: datetime | None = None, rpc: Rpc | None = None
) -> dict[str, Any]:
    if not memecoins_enabled():
        return {"status": "disabled"}
    started = at or datetime.now(UTC)
    # Share the request budget across worker processes and restarts.
    with connection() as database:
        claimed = database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES('memecoins_attempt','',?) "
            "ON CONFLICT(key) DO UPDATE SET updated_at=excluded.updated_at "
            "WHERE worker_state.updated_at<=? RETURNING key",
            (started.isoformat(), (started - timedelta(seconds=REFRESH_SECONDS)).isoformat()),
        ).fetchone()
    if not claimed:
        return {"status": "cached"}
    try:
        rows, metadata = _collect_helius(download=download or _download, at=started, rpc=rpc)
    except Exception as exc:
        LOG.exception("Memecoin refresh failed")
        run_id = record_source_fetch(
            SourceFetch.failure(
                source="helius",
                feed="memecoins",
                locator=RPC_URL + "#getTransactionsForAddress",
                started_at=started,
                error=exc,
            )
        )
        _save_state("memecoins_error", {"at": started.isoformat()}, started)
        return {"status": "error", "run_id": run_id}
    collected_at = at or datetime.now(UTC)
    run_id = record_source_fetch(
        SourceFetch.success(
            source="helius",
            feed="memecoins",
            locator=RPC_URL + "#getTransactionsForAddress",
            started_at=started,
            payload=rows,
            content_type="application/json",
            metadata={**metadata, "received_count": len(rows)},
            partial=metadata.get("partial", False),
        )
    )
    save_memecoin_snapshot(rows, run_id=run_id, collected_at=collected_at)
    return {"status": "ok", "count": len(rows), "run_id": run_id}


def _price_label(price: float) -> str:
    if price >= 1:
        return f"${price:,.2f}"
    if price < 0.00000001:
        return f"${price:.4g}"
    decimals = max(2, 3 - math.floor(math.log10(price)))
    return "$" + f"{price:.{decimals}f}".rstrip("0").rstrip(".")


def _amount_label(value: float | None) -> str:
    if value is None:
        return "—"
    for size, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= size:
            return f"${value / size:,.2f}{suffix}"
    return f"${value:,.2f}"


def pool_state(coin: dict[str, Any]) -> dict[str, Any] | None:
    """What the pool says about the coin in plain words, or None when nothing is wrong.

    Only the latest quote's liquidity and 24h change are read. A pool too thin
    to trade closes Calls; a collapse on a deep pool is only reported.
    """

    liquidity = _number(coin.get("liquidity_usd"), minimum=0)
    change = _number(coin.get("change_24h"))
    thin = liquidity is not None and liquidity < MIN_CALL_LIQUIDITY_USD
    left = f"{_amount_label(liquidity)} left in the pool"
    if change is not None and change <= COLLAPSE_CHANGE_PCT:
        text = f"Collapsed: down {abs(change):.2f}% in 24h" + (f", {left}." if thin else ".")
    elif thin and coin.get("venue") == "bonding_curve":
        # Nothing was drained: a curve starts small and fills as people buy.
        text = (
            f"Early bonding curve: {_amount_label(liquidity)} in the curve. "
            "One trade can move the price a long way."
        )
    elif thin:
        text = f"Thin pool: {left}. One trade can move the price a long way."
    else:
        return None
    return {"text": text, "calls_closed": thin}


def _market_states(*, keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    requested = keys or (
        "memecoins_snapshot",
        "memecoins_error",
        "memecoin_integrity_alerts",
        "memecoin_integrity_coverage",
        "memecoin_forensics",
        "memecoin_price_check",
    )
    placeholders = ",".join("?" for _ in requested)
    states = {}
    with connection() as database:
        for state in database.execute(
            f"SELECT key,value FROM worker_state WHERE key IN ({placeholders})", requested
        ).fetchall():
            try:
                states[state["key"]] = json.loads(state["value"])
            except (TypeError, ValueError):
                states[state["key"]] = None
    return states


def _quote_display(row: dict[str, Any], collected_at: Any, at: datetime) -> dict[str, Any]:
    row = dict(row)
    observed = _time(row.get("observed_at"))
    collected = _time(collected_at)
    row["stale"] = (
        collected is None
        or not 0 <= (at - collected).total_seconds() <= STALE_SECONDS
        or observed is None
        or not -60 <= (at - observed).total_seconds() <= STALE_SECONDS
    )
    row["price_label"] = _price_label(row["price"])
    row["volume_label"] = _amount_label(row["volume_24h"])
    row["market_cap_label"] = _amount_label(row["market_cap"])
    row["liquidity_label"] = _amount_label(_number(row.get("liquidity_usd"), minimum=0))
    row["fdv_label"] = _amount_label(_number(row.get("fully_diluted_valuation"), minimum=0))
    row["detail_url"] = f"/memecoins/coin/{row['id']}"
    if row.get("token_address"):
        # Rows saved before claimed_* existed carried an address prefix as symbol.
        row["symbol"] = short_address(row["token_address"])
        row["name"] = row["token_address"]
    return display_assessment(row, at=at)


def memecoin_market(
    *, query: str = "", sort: str = "volume", view: str = "radar", at: datetime | None = None
) -> dict[str, Any]:
    current = at or datetime.now(UTC)
    states = _market_states()
    snapshot = states.get("memecoins_snapshot") or {}
    rows = [
        _quote_display(row, snapshot.get("collected_at"), current)
        for row in snapshot.get("rows", [])
    ]
    findings = (states.get("memecoin_forensics") or {}).get("findings") or []
    for row in rows:
        row["findings"] = [
            finding
            for finding in findings
            if row.get("token_address") and finding.get("token_address") == row["token_address"]
        ]
    collected = _time(snapshot.get("collected_at"))
    stale = collected is None or not 0 <= (current - collected).total_seconds() <= STALE_SECONDS
    total = len(rows)
    status = "stale" if rows and (stale or all(row["stale"] for row in rows)) else "ok"
    if not rows:
        status = (
            "unavailable"
            if states.get("memecoins_error")
            else "ok"
            if collected is not None and not stale
            else "pending"
        )
    if not memecoins_enabled():
        status, rows = "disabled", []
    view = "pulse" if view == "pulse" else "radar"
    if view == "pulse":
        rows = [
            row
            for row in rows
            if not row["stale"] and row["change_24h"] is not None and row["volume_24h"] is not None
        ]
    query = query.strip()[:80]
    ranks: dict[str, int] = {}
    if query:
        for row in rows:
            rank = coin_search_rank(query, row)
            if rank is not None:
                ranks[row["id"]] = rank
        rows = [row for row in rows if row["id"] in ranks]
    sort = sort if sort in {"volume", "market_cap", "gainers", "losers"} else "volume"
    field = {"volume": "volume_24h", "gainers": "change_24h", "losers": "change_24h"}.get(
        sort, "market_cap"
    )
    rows.sort(
        key=lambda row: (
            # A creator-set name can be copied by any launch; address matches lead.
            ranks.get(row["id"], 0),
            row[field] is None,
            (row[field] or 0) * (1 if sort == "losers" else -1),
            row["id"],
        )
    )
    if view == "pulse":
        rows = rows[:20]
    return {
        "rows": rows,
        "total": total,
        "view": view,
        "visible_count": len(rows),
        "status": status,
        "query": query,
        "sort": sort,
        "collected_at": snapshot.get("collected_at"),
        "run_id": snapshot.get("run_id"),
        "refresh_failed": bool(states.get("memecoins_error")),
        "currency": "USD",
        "source": rows[0].get("source", "CoinGecko") if rows else SOURCE,
        "refresh_seconds": REFRESH_SECONDS,
        "discovery_source": "Helius",
        "integrity_alerts": (
            (states.get("memecoin_forensics") or {}).get("findings", [])
            if (states.get("memecoin_forensics") or {}).get("analyzed_events")
            else states.get("memecoin_integrity_alerts") or []
        ),
        "integrity_coverage": states.get("memecoin_integrity_coverage") or {},
        "forensics": states.get("memecoin_forensics") or {},
        # How far chain prices sit from GeckoTerminal's while both are read.
        "price_check": states.get("memecoin_price_check"),
        "price_source": price_source(),
    }


def snapshot_version() -> str:
    """Collected-at of the current quote snapshot; changes on every refresh.

    Public caches key on this so a refreshed snapshot is not served stale.
    """

    snapshot = _market_states(keys=("memecoins_snapshot",)).get("memecoins_snapshot") or {}
    return str(snapshot.get("collected_at") or "")


def memecoin_detail(
    coin_id: str, *, at: datetime | None = None, history_limit: int = 288
) -> dict[str, Any] | None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", coin_id):
        return None
    current = at or datetime.now(UTC)
    states = _market_states(keys=("memecoins_snapshot", "memecoins_error", "memecoin_forensics"))
    snapshot = states.get("memecoins_snapshot") or {}
    snapshot_coin = next((row for row in snapshot.get("rows", []) if row["id"] == coin_id), None)
    saved = stored_memecoin(coin_id)
    if saved is None:
        if snapshot_coin is None:
            return None
        saved = {
            "coin": snapshot_coin,
            "collected_at": snapshot.get("collected_at"),
            "run_id": snapshot.get("run_id"),
        }
    coin = _quote_display(saved["coin"], saved["collected_at"], current)
    coin["findings"] = [
        finding
        for finding in (states.get("memecoin_forensics") or {}).get("findings") or []
        if coin.get("token_address") and finding.get("token_address") == coin["token_address"]
    ]
    active = snapshot_coin is not None
    coin["stale"] = coin["stale"] or not memecoins_enabled()
    coin = display_assessment(coin, at=current)
    status = "stale" if coin["stale"] else "ok"
    if not memecoins_enabled():
        status = "disabled"
    return {
        "coin": coin,
        "status": status,
        "collected_at": saved["collected_at"],
        "refresh_failed": bool(states.get("memecoins_error")),
        "source": coin.get("source", "CoinGecko"),
        "currency": "USD",
        "history": memecoin_history(coin_id, at=current, limit=history_limit),
        "states": memecoin_state_changes(coin_id, at=current),
        "evidence": {
            "source_url": coin["source_url"],
            "run_id": saved["run_id"],
            "observed_at": coin.get("observed_at"),
            "collected_at": saved["collected_at"],
            "checks": {
                "source_time_known": coin.get("observed_at") is not None,
                "quote_fresh": not coin["stale"],
                "volume_known": coin.get("volume_24h") is not None,
                "market_cap_known": coin.get("market_cap") is not None,
            },
        },
        "in_current_snapshot": active,
    }
