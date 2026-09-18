"""Read-only Robinhood Chain Stock Token data for stock detail pages.

This is curated enrichment, not ingestion. A stock detail can name the ticker's
Robinhood Chain token (EIP-155 chain 4663), show its contract address, current
multiplier, live token quote and recent corporate actions, and carry the legal
disclosure that the token is a debt security rather than the underlying shares.

Everything comes from Robinhood's public read-only Stock Token API and is cached
in-process. A page render never waits on the network: it reads the cache and, at
most, schedules a background refresh. Failures keep the previous snapshot, and
nothing here writes to the database or touches the chain.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

LOG = logging.getLogger(__name__)

API_ROOT = "https://api.robinhood.com/rhj"
ASSETS_URL = f"{API_ROOT}/assets"
PRICES_URL = f"{API_ROOT}/prices"
CORPORATE_ACTIONS_URL = f"{API_ROOT}/corporate-actions"
DOCS_URL = "https://docs.robinhood.com/chain/stock-tokens"
CHAIN_ID = 4663
USER_AGENT = "RunnerWatch/0.3 https://stonks.rati.foundation"

Transport = Callable[[str, dict[str, str], float], Any]

_DEFAULT_CACHE_SECONDS = 300.0
_DEFAULT_PRICE_CACHE_SECONDS = 15.0
_DEFAULT_ACTIONS_CACHE_SECONDS = 3600.0
_DEFAULT_TIMEOUT_SECONDS = 4.0

ACTION_LABELS = {
    "forward_split": "Forward split",
    "reverse_split": "Reverse split",
    "cash_dividend": "Cash dividend",
    "stock_dividend": "Stock dividend",
    "spin_off": "Spin-off",
    "cash_merger": "Cash merger",
    "stock_merger": "Stock merger",
    "stock_and_cash_merger": "Stock and cash merger",
    "redemption": "Redemption",
    "name_change": "Name change",
    "worthless_removal": "Worthless removal",
    "rights_distribution": "Rights distribution",
    "unit_split": "Unit split",
}

_lock = threading.Lock()
_state: dict[str, Any] = {"assets": None, "at": 0.0, "refreshing": False}

_prices_lock = threading.Lock()
_prices: dict[str, dict[str, Any]] = {}
_prices_inflight: set[str] = set()

_actions_lock = threading.Lock()
_actions: dict[str, list[dict[str, Any]]] = {}
_actions_at = 0.0
_actions_inflight = False

_transport_override: Transport | None = None


def robinhood_chain_enabled() -> bool:
    value = os.getenv("ROBINHOOD_CHAIN_ENABLED", "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _cache_seconds() -> float:
    return _float_env("ROBINHOOD_CHAIN_CACHE_SECONDS", _DEFAULT_CACHE_SECONDS)


def _price_cache_seconds() -> float:
    return _float_env("ROBINHOOD_CHAIN_PRICE_CACHE_SECONDS", _DEFAULT_PRICE_CACHE_SECONDS)


def _actions_cache_seconds() -> float:
    return _float_env("ROBINHOOD_CHAIN_ACTIONS_CACHE_SECONDS", _DEFAULT_ACTIONS_CACHE_SECONDS)


def _timeout_seconds() -> float:
    value = _float_env("ROBINHOOD_CHAIN_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS)
    return min(max(value, 0.5), 10.0)


def _default_transport(url: str, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _transport(transport: Transport | None) -> Transport:
    return transport or _transport_override or _default_transport


def set_transport(transport: Transport | None) -> None:
    """Test hook: route background refreshes through a fake transport."""

    global _transport_override
    _transport_override = transport


def _normalize_symbol(value: Any) -> str:
    return "".join(
        character
        for character in str(value or "").upper()
        if character.isalnum() or character in ".-"
    )


def _chain_id(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_assets(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("assets"), list):
        return {}
    by_symbol: dict[str, dict[str, Any]] = {}
    for asset in payload["assets"]:
        if not isinstance(asset, dict):
            continue
        symbol = str(asset.get("tokenSymbol") or "").strip().upper()
        if not symbol:
            continue
        deployments = [row for row in asset.get("deployments") or [] if isinstance(row, dict)]
        deployment = next(
            (row for row in deployments if _chain_id(row.get("chainId")) == CHAIN_ID),
            deployments[0] if deployments else None,
        )
        if not deployment or not deployment.get("contractAddress"):
            continue
        by_symbol[symbol] = {
            "symbol": symbol,
            "name": str(asset.get("tokenName") or symbol),
            "contract_address": str(deployment["contractAddress"]),
            "chain_id": _chain_id(deployment.get("chainId")) or CHAIN_ID,
            "multiplier": str(asset.get("currentMultiplier") or ""),
            "pending_multiplier": str(asset.get("pendingMultiplier") or ""),
            "status": str(asset.get("status") or "").removeprefix("ASSET_STATUS_").lower(),
            "logo_url": str(asset.get("logoUrl") or ""),
            "docs_url": DOCS_URL,
        }
    return by_symbol


def refresh_assets(
    *,
    transport: Transport | None = None,
    now: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch and cache the Stock Token asset list. Safe to call eagerly in tests."""

    try:
        payload = _transport(transport)(
            ASSETS_URL,
            {"Accept": "application/json", "User-Agent": USER_AGENT},
            _timeout_seconds(),
        )
        assets = _normalize_assets(payload)
        if not assets:
            raise ValueError("asset payload contained no usable deployments")
    except Exception as exc:  # noqa: BLE001 - any failure keeps the last snapshot
        LOG.warning("Robinhood Chain asset refresh failed: %s", exc)
        with _lock:
            return _state.get("assets") or {}
    with _lock:
        _state["assets"] = assets
        _state["at"] = time.monotonic() if now is None else now
    return assets


def _background_refresh() -> None:
    try:
        refresh_assets()
    finally:
        with _lock:
            _state["refreshing"] = False


def _schedule_refresh() -> None:
    with _lock:
        if _state["refreshing"]:
            return
        _state["refreshing"] = True
    threading.Thread(
        target=_background_refresh,
        name="robinhood-chain-assets",
        daemon=True,
    ).start()


def _normalize_price(payload: Any, symbol: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("quotes"), list):
        return None
    for quote in payload["quotes"]:
        if not isinstance(quote, dict):
            continue
        if _normalize_symbol(quote.get("tokenSymbol")) != symbol:
            continue
        return {
            "symbol": symbol,
            "bid": str(quote.get("bid") or ""),
            "ask": str(quote.get("ask") or ""),
            "currency": str(quote.get("currency") or ""),
            "volume": str(quote.get("dailyTradingVolume") or ""),
            "halt": bool(quote.get("isTradingHalt")),
            "as_of": str(quote.get("generatedAt") or ""),
        }
    return None


def refresh_price(
    symbol: str,
    *,
    transport: Transport | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Fetch and cache one token quote. Never raises."""

    symbol = _normalize_symbol(symbol)
    if not symbol:
        return None
    try:
        payload = _transport(transport)(
            f"{PRICES_URL}/{urllib.parse.quote(symbol, safe='')}",
            {"Accept": "application/json", "User-Agent": USER_AGENT},
            _timeout_seconds(),
        )
        price = _normalize_price(payload, symbol)
        if price is None:
            raise ValueError("price payload did not match the requested symbol")
    except Exception as exc:  # noqa: BLE001 - any failure keeps the last quote
        LOG.warning("Robinhood Chain price refresh failed for %s: %s", symbol, exc)
        with _prices_lock:
            entry = _prices.get(symbol)
            return dict(entry["value"]) if entry else None
    with _prices_lock:
        _prices[symbol] = {"at": time.monotonic() if now is None else now, "value": price}
    return price


def _background_price(symbol: str) -> None:
    try:
        refresh_price(symbol)
    finally:
        with _prices_lock:
            _prices_inflight.discard(symbol)


def _schedule_price(symbol: str) -> None:
    with _prices_lock:
        if symbol in _prices_inflight:
            return
        _prices_inflight.add(symbol)
    threading.Thread(
        target=_background_price,
        args=(symbol,),
        name=f"robinhood-chain-price-{symbol}",
        daemon=True,
    ).start()


def _normalize_actions(payload: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("corpActions"), list):
        return {}
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for action in payload["corpActions"]:
        if not isinstance(action, dict):
            continue
        symbol = str(action.get("tokenSymbol") or "").strip().upper()
        if not symbol:
            continue
        kind = str(action.get("type") or "").removeprefix("CORPORATE_ACTION_TYPE_").lower()
        process = action.get("processDate") if isinstance(action.get("processDate"), dict) else {}
        date = ""
        try:
            date = (
                f"{int(process['year']):04d}-{int(process['month']):02d}-"
                f"{int(process['day']):02d}"
            )
        except (KeyError, TypeError, ValueError):
            date = ""
        by_symbol.setdefault(symbol, []).append(
            {
                "type": kind,
                "label": ACTION_LABELS.get(kind, kind.replace("_", " ").capitalize() or "Action"),
                "date": date,
                "status": str(action.get("status") or "")
                .removeprefix("CORPORATE_ACTION_STATUS_")
                .lower(),
            }
        )
    for rows in by_symbol.values():
        rows.sort(key=lambda item: item["date"], reverse=True)
    return by_symbol


def refresh_actions(
    *,
    transport: Transport | None = None,
    now: float | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch and cache processed corporate actions. Never raises."""

    global _actions_at
    try:
        payload = _transport(transport)(
            CORPORATE_ACTIONS_URL,
            {"Accept": "application/json", "User-Agent": USER_AGENT},
            _timeout_seconds(),
        )
        actions = _normalize_actions(payload)
        if not actions:
            raise ValueError("corporate action payload was empty")
    except Exception as exc:  # noqa: BLE001 - any failure keeps the last snapshot
        LOG.warning("Robinhood Chain corporate action refresh failed: %s", exc)
        with _actions_lock:
            return {symbol: [dict(row) for row in rows] for symbol, rows in _actions.items()}
    with _actions_lock:
        _actions.clear()
        _actions.update(actions)
        _actions_at = time.monotonic() if now is None else now
    return actions


def _background_actions() -> None:
    global _actions_inflight
    try:
        refresh_actions()
    finally:
        with _actions_lock:
            _actions_inflight = False


def _schedule_actions() -> None:
    global _actions_inflight
    with _actions_lock:
        if _actions_inflight:
            return
        _actions_inflight = True
    threading.Thread(
        target=_background_actions,
        name="robinhood-chain-actions",
        daemon=True,
    ).start()


def price_for(symbol: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Return the cached token quote, or None. Never blocks."""

    if not robinhood_chain_enabled():
        return None
    symbol = _normalize_symbol(symbol)
    if not symbol:
        return None
    timestamp = time.monotonic() if now is None else now
    with _prices_lock:
        entry = _prices.get(symbol)
        fresh = entry is not None and (timestamp - float(entry["at"])) < _price_cache_seconds()
        inflight = symbol in _prices_inflight
    if not fresh and not inflight:
        _schedule_price(symbol)
    return dict(entry["value"]) if entry else None


def actions_for(symbol: str, *, now: float | None = None) -> list[dict[str, Any]]:
    """Return cached corporate actions for a symbol. Never blocks."""

    if not robinhood_chain_enabled():
        return []
    symbol = _normalize_symbol(symbol)
    if not symbol:
        return []
    timestamp = time.monotonic() if now is None else now
    with _actions_lock:
        fresh = bool(_actions) and (timestamp - _actions_at) < _actions_cache_seconds()
        inflight = _actions_inflight
    if not fresh and not inflight:
        _schedule_actions()
    with _actions_lock:
        return [dict(row) for row in _actions.get(symbol, [])]


def stock_token(ticker: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Return the ticker's Robinhood Chain token, or None. Never blocks."""

    if not robinhood_chain_enabled():
        return None
    symbol = _normalize_symbol(ticker)
    if not symbol:
        return None
    timestamp = time.monotonic() if now is None else now
    with _lock:
        assets = _state.get("assets")
        fresh = assets is not None and (timestamp - float(_state["at"])) < _cache_seconds()
        refreshing = bool(_state["refreshing"])
    if not fresh and not refreshing:
        _schedule_refresh()
    if not assets:
        return None
    token = assets.get(symbol)
    if not token:
        return None
    result = dict(token)
    result["price"] = price_for(symbol, now=timestamp)
    result["actions"] = actions_for(symbol, now=timestamp)
    return result


def reset_cache() -> None:
    """Test helper: drop every cached snapshot and in-flight marker."""

    global _actions_at, _actions_inflight
    with _lock:
        _state.update({"assets": None, "at": 0.0, "refreshing": False})
    with _prices_lock:
        _prices.clear()
        _prices_inflight.clear()
    with _actions_lock:
        _actions.clear()
        _actions_at = 0.0
        _actions_inflight = False
    set_transport(None)
