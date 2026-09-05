from __future__ import annotations

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
from runner_web.ingestion import record_source_fetch
from runner_web.memecoin_store import memecoin_history, save_memecoin_snapshot, stored_memecoin

MARKETS_URL = (
    "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd"
    "&category=meme-token&order=market_cap_desc&per_page=100&page=1"
    "&sparkline=false&precision=full"
)
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
                "detail_url": f"/memecoins/{coin_id}",
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
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, json.dumps(value, allow_nan=False), at.isoformat()),
        )


def refresh_memecoins(
    *, download: Download | None = None, at: datetime | None = None
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
        body = (download or _download)(MARKETS_URL, 10.0)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Memecoin response exceeds the size limit")
        payload = json.loads(body)
        rows = normalize_memecoins(payload)
    except Exception as exc:
        run_id = record_source_fetch(
            SourceFetch.failure(
                source="coingecko",
                feed="memecoins",
                locator=MARKETS_URL,
                started_at=started,
                error=exc,
            )
        )
        _save_state("memecoins_error", {"at": started.isoformat()}, started)
        return {"status": "error", "run_id": run_id}
    collected_at = at or datetime.now(UTC)
    run_id = record_source_fetch(
        SourceFetch.success(
            source="coingecko",
            feed="memecoins",
            locator=MARKETS_URL,
            started_at=started,
            payload=rows,
            content_type="application/json",
            metadata={"received_count": len(rows), "requested_count": 100},
            partial=len(rows) < len(payload),
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
            "WHERE key IN ('memecoins_snapshot','memecoins_error')"
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
    row["detail_url"] = f"/memecoins/{row['id']}"
    return row


def memecoin_market(
    *, query: str = "", sort: str = "volume", at: datetime | None = None
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
        status = "unavailable" if states.get("memecoins_error") else "pending"
    if not memecoins_enabled():
        status, rows = "disabled", []
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
    return {
        "rows": rows,
        "total": total,
        "status": status,
        "query": query,
        "sort": sort,
        "collected_at": snapshot.get("collected_at"),
        "run_id": snapshot.get("run_id"),
        "refresh_failed": bool(states.get("memecoins_error")),
        "currency": "USD",
        "source": "CoinGecko",
        "refresh_seconds": REFRESH_SECONDS,
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
        "source": "CoinGecko",
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
