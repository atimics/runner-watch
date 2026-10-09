"""Contract addresses submitted to Dash enter the website's search queue."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from runner_web.helius_discovery import _address

MAX_ADDRESSES = 5
ADDRESS = re.compile(r"(?<![A-Za-z0-9_])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9_])")
URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def extract_addresses(text: str, entities: Any = ()) -> tuple[str, ...]:
    """Read full Solana addresses and links whose path identifies a token."""
    links = URL.findall(text)
    links += [
        str(item.get("url") or "")
        for item in entities
        if isinstance(item, dict) and item.get("type") == "text_link"
    ]
    candidates = ADDRESS.findall(URL.sub(" ", text))
    for link in links:
        try:
            parsed = urlsplit(link.rstrip(".,;!)]"))
        except ValueError:
            continue
        parts = parsed.path.strip("/").split("/")
        prefixes = {"pump.fun": ["coin"], "solscan.io": ["token"], "gmgn.ai": ["sol", "token"]}
        prefix = prefixes.get((parsed.hostname or "").removeprefix("www."))
        if prefix and parts[:-1] == prefix:
            candidates.append(parts[-1])
    found = []
    for candidate in candidates:
        try:
            _address(candidate)
        except ValueError:
            continue
        if candidate not in found:
            found.append(candidate)
    return tuple(found)


def forward_source(message: dict[str, Any]) -> dict[str, Any]:
    origin = message.get("forward_origin") or {}
    if not isinstance(origin, dict):
        origin = {}
    channel = origin.get("chat") or origin.get("sender_chat") or message.get("forward_from_chat")
    channel = channel if isinstance(channel, dict) else {}
    return {
        "kind": origin.get("type") or ("chat" if channel else "submitted"),
        "title": str(channel.get("title") or "")[:120],
        "chat_id": channel.get("id"),
        "message_id": origin.get("message_id") or message.get("forward_from_message_id"),
        "date": origin.get("date") or message.get("forward_date"),
    }


def ingest_message(message: Any, *, at: datetime) -> Any:
    """Use the same saved data and search queue as the memecoin board."""
    from runner_web.dash import coin_detail

    coins = tuple(coin_detail(address, at=at) for address in message.addresses[:MAX_ADDRESSES])
    return replace(message, coin_lookups=coins)


def intake_reply(message: Any) -> dict[str, str]:
    """A short receipt for forwarded CAs, based on the shared website data."""
    lines = []
    for coin in message.coin_lookups:
        lines.append(coin.get("contract_address") or coin["query"])
        if coin.get("requested"):
            lines.append("Added to the Runners assessment queue, like a website search.")
        if coin.get("known"):
            if coin.get("stale"):
                lines.append("The website's saved assessment is waiting for fresh data.")
            else:
                lines.append("Website price: " + str(coin.get("price") or "pending"))
                rules = coin.get("ratification") or {}
                if rules.get("ratified"):
                    lines.append("RATi checks: Ratified.")
                elif rules:
                    lines.append(f"RATi checks: {rules.get('met', 0)}/{rules.get('total', 0)} met.")
                if coin.get("attention_score") is not None:
                    lines.append(f"Website activity score: {coin['attention_score']}/100.")
                lines.extend(str(label) for label in coin.get("risks", [])[:2])
        elif not coin.get("requested"):
            lines.append(
                "The shared lookup queue is busy or this address is in its retry window. "
                "Try again later."
            )
    if len(message.addresses) > MAX_ADDRESSES:
        lines.append("I checked the first five CAs. Send the remaining CAs in another message.")
    return {"action": "reply", "text": "\n".join(lines)}
