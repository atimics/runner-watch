"""Read-only Robinhood Chain Stock Token metadata for stock detail pages.

This is curated enrichment, not ingestion. A stock detail can name the ticker's
Robinhood Chain token (EIP-155 chain 4663), show its contract address and
current multiplier, and carry the legal disclosure that the token is a debt
security rather than the underlying shares.

Metadata comes from Robinhood's public read-only Stock Token API and is cached
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
import urllib.request
from collections.abc import Callable
from typing import Any

LOG = logging.getLogger(__name__)

API_ROOT = "https://api.robinhood.com/rhj"
ASSETS_URL = f"{API_ROOT}/assets"
DOCS_URL = "https://docs.robinhood.com/chain/stock-tokens"
CHAIN_ID = 4663
USER_AGENT = "RunnerWatch/0.3 https://stonks.rati.foundation"

Transport = Callable[[str, dict[str, str], float], Any]

_DEFAULT_CACHE_SECONDS = 300.0
_DEFAULT_TIMEOUT_SECONDS = 4.0

_lock = threading.Lock()
_state: dict[str, Any] = {"assets": None, "at": 0.0, "refreshing": False}


def robinhood_chain_enabled() -> bool:
    value = os.getenv("ROBINHOOD_CHAIN_ENABLED", "0").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _cache_seconds() -> float:
    try:
        value = float(os.getenv("ROBINHOOD_CHAIN_CACHE_SECONDS", "") or _DEFAULT_CACHE_SECONDS)
    except ValueError:
        return _DEFAULT_CACHE_SECONDS
    return value if value > 0 else _DEFAULT_CACHE_SECONDS


def _timeout_seconds() -> float:
    try:
        value = float(os.getenv("ROBINHOOD_CHAIN_TIMEOUT_SECONDS", "") or _DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS
    return min(max(value, 0.5), 10.0)


def _default_transport(url: str, headers: dict[str, str], timeout: float) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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
        payload = (transport or _default_transport)(
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


def stock_token(ticker: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Return the ticker's Robinhood Chain token, or None. Never blocks."""

    if not robinhood_chain_enabled():
        return None
    symbol = "".join(
        character
        for character in str(ticker or "").upper()
        if character.isalnum() or character in ".-"
    )
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
    return dict(token) if token else None


def reset_cache() -> None:
    """Test helper: drop the cached snapshot and any in-flight marker."""

    with _lock:
        _state.update({"assets": None, "at": 0.0, "refreshing": False})
