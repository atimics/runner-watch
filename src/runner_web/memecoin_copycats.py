"""Copied launch names point at the coin being copied.

When several new launches take the same name, scammers are chasing attention
that an original already has, as with "trolloween" copies each Halloween. The
name finds candidates only: the original is the coin, by contract address,
whose pools are at least a week older than the copies and hold the most
liquidity. An older copy that died has little liquidity and loses. Launches
that copy a verified original are marked; without one, nothing is marked.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

LOG = logging.getLogger(__name__)
SEARCH_URL = "https://api.geckoterminal.com/api/v2/search/pools?"
MIN_COPIES = 3
BURST_HOURS = 24
MIN_ORIGINAL_AGE = timedelta(days=7)
MIN_ORIGINAL_LIQUIDITY_USD = 5_000.0
SEARCH_TTL = timedelta(hours=6)
MAX_SEARCHES = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# Look-alike characters read as one: "Tr0lloween" and "TRO11OWEEN" are "trolloween".
_LOOKALIKES = str.maketrans("01l|34@57", "oiiieaast")


def name_key(text: Any) -> str:
    """A launch name folded for matching copies; empty when too short to mean anything."""

    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    folded = folded.translate(_LOOKALIKES)
    key = re.sub(r"[^a-z0-9]", "", folded)
    return key if len(key) >= 3 else ""


def copycat_bursts(events: list[dict[str, Any]], at: datetime) -> dict[str, dict[str, Any]]:
    """Names that at least three distinct new launches took in the last day."""

    groups: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("kind") != "token_launch" or not event.get("token_address"):
            continue
        try:
            observed = datetime.fromisoformat(str(event["observed_at"]))
        except (KeyError, ValueError):
            continue
        age = at - observed
        if not timedelta(0) <= age <= timedelta(hours=BURST_HOURS):
            continue
        # One name per launch: copies share the name, while a short symbol like
        # TROLL would tie them to an unrelated coin.
        text = next(
            (
                text
                for text in (event.get("claimed_name"), event.get("claimed_symbol"))
                if name_key(text)
            ),
            None,
        )
        if text is None:
            continue
        key = name_key(text)
        group = groups.setdefault(key, {"key": key, "launches": {}, "texts": {}})
        launch = group["launches"].setdefault(event["token_address"], event)
        if observed < datetime.fromisoformat(str(launch["observed_at"])):
            group["launches"][event["token_address"]] = event
        group["texts"][text] = group["texts"].get(text, 0) + 1
    bursts = {}
    for key, group in groups.items():
        launches = group["launches"]
        if len(launches) < MIN_COPIES:
            continue
        recent = [
            launch
            for launch in launches.values()
            if at - datetime.fromisoformat(str(launch["observed_at"])) <= timedelta(hours=1)
        ]
        bursts[key] = {
            "key": key,
            # Search the commonest spelling, preferring one without digit swaps.
            "query": max(
                group["texts"],
                key=lambda text: (group["texts"][text], not re.search(r"\d", text), text),
            ),
            "launches": launches,
            "h1": len(recent),
            "h24": len(launches),
        }
    return bursts


def _original(
    key: str, body: dict[str, Any], at: datetime, copies: set[str]
) -> dict[str, Any] | None:
    """The oldest-established, best-funded coin in a name search, by address."""

    names = {
        item.get("id", "")[len("solana_") :]: item.get("attributes") or {}
        for item in body.get("included") or []
        if item.get("type") == "token" and str(item.get("id", "")).startswith("solana_")
    }
    coins: dict[str, dict[str, Any]] = {}
    for pool in body.get("data") or []:
        try:
            attrs = pool["attributes"]
            mint = pool["relationships"]["base_token"]["data"]["id"][len("solana_") :]
            created = datetime.fromisoformat(attrs["pool_created_at"].replace("Z", "+00:00"))
            liquidity = float(attrs.get("reserve_in_usd") or 0)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        token = names.get(mint, {})
        if mint in copies or key not in {
            name_key(token.get("name")),
            name_key(token.get("symbol")),
        }:
            continue
        coin = coins.setdefault(
            mint, {"token_address": mint, "liquidity_usd": 0.0, "created_at": created}
        )
        coin["liquidity_usd"] += max(liquidity, 0.0)
        coin["created_at"] = min(coin["created_at"], created)
        if liquidity >= coin.get("pool_liquidity_usd", -1.0):
            coin.update(pool_address=attrs["address"], pool_liquidity_usd=liquidity)
    eligible = [
        coin
        for coin in coins.values()
        if at - coin["created_at"] >= MIN_ORIGINAL_AGE
        and coin["liquidity_usd"] >= MIN_ORIGINAL_LIQUIDITY_USD
    ]
    if not eligible:
        return None
    best = max(eligible, key=lambda coin: (coin["liquidity_usd"], coin["token_address"]))
    return {**best, "created_at": best["created_at"].isoformat()}


def find_originals(
    bursts: dict[str, dict[str, Any]],
    cache: dict[str, Any],
    *,
    download: Callable[[str, float], bytes],
    at: datetime,
    pause: float,
    max_searches: int = MAX_SEARCHES,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], int]:
    """Originals for current bursts, searching at most a few names a cycle.

    Results are cached for six hours, found or not, and each search waits
    `pause` first. A failed search skips the name this cycle; it never fails
    the refresh. With `max_searches=0` only the cache is read.
    """

    fresh: dict[str, Any] = {}
    for key, entry in cache.items():
        try:
            checked = datetime.fromisoformat(str(entry["checked_at"]))
        except (KeyError, TypeError, ValueError):
            continue
        if at - checked <= SEARCH_TTL:
            fresh[key] = entry
    searches = 0
    for key, burst in sorted(bursts.items(), key=lambda item: (-item[1]["h24"], item[0])):
        if key in fresh or searches >= max_searches:
            continue
        time.sleep(pause)
        searches += 1
        query = urlencode({"query": burst["query"], "network": "solana", "include": "base_token"})
        try:
            raw = download(SEARCH_URL + query, 10.0)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Search response exceeds the size limit")
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Search response is invalid")
        except Exception:
            LOG.warning("Copycat original search failed for %r", key, exc_info=True)
            continue
        fresh[key] = {
            "checked_at": at.isoformat(),
            "original": _original(key, body, at, set(burst["launches"])),
        }
    originals = {
        key: fresh[key]["original"] for key in bursts if key in fresh and fresh[key].get("original")
    }
    return originals, fresh, searches


def mark_copycats(
    rows: list[dict[str, Any]],
    bursts: dict[str, dict[str, Any]],
    originals: dict[str, dict[str, Any]],
) -> None:
    """Give an original its copy counts and each copy the original it copies."""

    by_token = {row.get("token_address"): row for row in rows}
    for key, original in originals.items():
        burst = bursts[key]
        row = by_token.get(original["token_address"])
        if row is not None and (row.get("copycats") or {}).get("h24", 0) < burst["h24"]:
            row["copycats"] = {
                "h1": burst["h1"],
                "h24": burst["h24"],
                "evidence": [
                    {"signature": launch["signature"], "source_url": launch["source_url"]}
                    for launch in list(burst["launches"].values())[:10]
                    if launch.get("signature") and launch.get("source_url")
                ],
            }
        for mint in burst["launches"]:
            copy = by_token.get(mint)
            if copy is not None and mint != original["token_address"]:
                copy["copies"] = {"token_address": original["token_address"]}
