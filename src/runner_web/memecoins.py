from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_watch.ingestion import SourceFetch
from runner_watch.xml_security import read_limited
from runner_web.db import connection
from runner_web.helius_discovery import RPC_URL, Rpc
from runner_web.ingestion import record_source_fetch
from runner_web.memecoin_chain_ingestion import collect_chain as discover_pools
from runner_web.memecoin_forensics import analyze_events
from runner_web.memecoin_integrity import creator_trades
from runner_web.memecoin_store import memecoin_history, save_memecoin_snapshot, stored_memecoin

POOL_QUOTES_URL = "https://api.geckoterminal.com/api/v2/networks/solana/pools/multi/"
SOURCE = "GeckoTerminal"
MIN_POOL_LIQUIDITY_USD = 1000
REFRESH_SECONDS = 300
STALE_SECONDS = 900
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


def normalize_chain_pools(payload: Any, *, at: datetime) -> list[dict[str, Any]]:
    """Discover from pool records. Token identity and selection use chain fields only."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Expected DEX pool records")
    if len(payload["data"]) > 100:
        raise ValueError("Expected up to 100 pool records")
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
                "symbol": token[:8],
                "name": f"{network} · {token}",
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


def _collect_helius(
    *, download: Download, at: datetime, rpc: Rpc | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    discovery = discover_pools(at=at, rpc=rpc)
    with connection() as database:
        saved = database.execute(
            "SELECT value FROM worker_state WHERE key='helius_discovered_pools'"
        ).fetchone()
    try:
        previous = json.loads(saved["value"]) if saved else []
    except (ValueError, TypeError):
        previous = []
    candidates = {}
    for candidate in discovery["pools"] + previous:
        created = _time(candidate.get("created_at"))
        if created and 0 <= (at - created).total_seconds() <= 30 * 86400:
            candidates.setdefault(candidate["pool_address"], candidate)
    selected = sorted(candidates.values(), key=lambda item: item["created_at"], reverse=True)[:100]
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
    _save_state(
        "memecoin_integrity_coverage",
        {
            "checked_at": at.isoformat(),
            "program": "Pump, PumpSwap, Raydium CPMM",
            "streams": discovery.get("coverage", {}).get("streams", []),
            "budget": discovery.get("coverage", {}).get("budget", {}),
            "recorded_coverage_gaps": discovery.get("coverage", {}).get(
                "recorded_coverage_gaps", 0
            ),
            "commitment": "finalized",
            "received_transactions": discovery.get("received_transactions", 0),
            "partial": discovery.get("partial", False),
            "mode": "resumable",
        },
        at,
    )
    allowed = {item["pool_address"]: item for item in selected}
    payload = []
    addresses = list(allowed)
    for offset in range(0, len(addresses), 30):
        url = "https://api.geckoterminal.com/api/v2/networks/solana/pools/multi/" + ",".join(
            addresses[offset : offset + 30]
        )
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
    rows = normalize_chain_pools({"data": payload}, at=at)
    for row in rows:
        row["discovery"] = allowed[row["pool_address"]]
        row["discovery_source"] = "Helius"
    metadata = {
        key: value
        for key, value in discovery.items()
        if key not in {"pools", "transactions", "events"}
    }
    metadata.update(
        discovered_pools=len(discovery["pools"]),
        tracked_pools=len(selected),
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


def _market_states() -> dict[str, Any]:
    states = {}
    with connection() as database:
        for state in database.execute(
            "SELECT key,value FROM worker_state "
            "WHERE key IN ('memecoins_snapshot','memecoins_error',"
            "'memecoin_integrity_alerts','memecoin_integrity_coverage','memecoin_forensics')"
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
    row["detail_url"] = f"/memecoins/coin/{row['id']}"
    return row


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
    if query:
        rows = [
            row
            for row in rows
            if query.casefold() in (f"{row['id']} {row['symbol']} {row['name']}".casefold())
        ]
    sort = sort if sort in {"volume", "market_cap", "gainers", "losers"} else "volume"
    field = {"volume": "volume_24h", "gainers": "change_24h", "losers": "change_24h"}.get(
        sort, "market_cap"
    )
    rows.sort(
        key=lambda row: (
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
    }


def memecoin_detail(
    coin_id: str, *, at: datetime | None = None, history_limit: int = 288
) -> dict[str, Any] | None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", coin_id):
        return None
    current = at or datetime.now(UTC)
    states = _market_states()
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
    active = snapshot_coin is not None
    coin["stale"] = coin["stale"] or not memecoins_enabled()
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
