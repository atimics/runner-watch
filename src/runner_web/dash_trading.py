"""Private trading tools for Dash's owner."""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

from runner_trading import proposals
from runner_trading.dashboard import snapshot

TOOLS = (
    {
        "name": "trading_status",
        "description": "Read Dash's funded live wallet, positions and trade proposals in "
        "the owner's private chat.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "propose_trade",
        "description": "Propose a real Solana trade for the owner's approval. Use a full "
        "token address, your researched view, and what would change it. A "
        "buy uses the given USDC amount; a sale closes a held position. "
        "Return the review link and proposal state.",
        "parameters": {
            "type": "object",
            "properties": {
                "mint": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "amount_usdc": {
                    "type": "string",
                    "description": "USDC to spend on a buy, with up to six decimal places.",
                },
                "thesis": {"type": "string"},
                "invalidation": {"type": "string"},
            },
            "required": ["mint", "side", "thesis", "invalidation"],
        },
    },
)
NAMES = frozenset(t["name"] for t in TOOLS)


def allowed(message) -> bool:
    owners = {s.strip() for s in os.getenv("DASH_TRADING_TELEGRAM_IDS", "").split(",") if s.strip()}
    return bool(
        message.chat_type == "private"
        and message.user_id is not None
        and message.chat_id == message.user_id
        and not message.sender_is_bot
        and str(message.user_id) in owners
    )


def act(message, name: str, args: dict) -> dict:
    if not allowed(message):
        return {"error": "Use the configured owner's private chat for the live trading desk."}
    try:
        if name == "trading_status":
            return {
                **snapshot("live"),
                "proposals": proposals.listing(),
                "wallet": os.getenv("TRADING_WALLET", ""),
                "approval": "Each buy and sale needs approval on the trading desk.",
            }
        amount = None
        if args.get("side") == "buy":
            value = Decimal(str(args.get("amount_usdc", ""))) * 1_000_000
            if not value.is_finite() or value <= 0 or value != value.to_integral_value():
                raise ValueError("Choose a positive USDC amount with up to six decimal places")
            amount = int(value)
        p = proposals.create(
            source=f"telegram:{message.chat_id}:{message.update_id}",
            mint=str(args.get("mint", "")),
            side=str(args.get("side", "")),
            amount=amount,
            thesis=str(args.get("thesis", "")),
            invalidation=str(args.get("invalidation", "")),
        )
        return {
            "proposal_id": p["id"],
            "state": p["status"],
            "mint": p["mint"],
            "side": p["side"],
            "review_url": os.getenv("RUNNERS_ORIGIN", "https://runners.rati.chat").rstrip("/")
            + "/trading?mode=live",
            "next_step": "Review a fresh quote and approve this trade on the trading desk.",
        }
    except (ValueError, LookupError, InvalidOperation):
        return {
            "error": "Check the funded account, token address, amount and current "
            "position on the trading desk."
        }
