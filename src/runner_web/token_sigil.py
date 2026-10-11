"""Fill the existing token detail page while its full readings arrive."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ratitrust.memecoin import standards
from runner_web.memecoins import _chain_row
from runner_web.token_discoveries import coin_id


def pending_detail(mint: str, basics: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    coin = _chain_row(
        {"token_address": mint, "pool_address": ""},
        {"price": basics.get("price"), "liquidity_usd": None},
        now,
    )
    coin.update(
        id=coin_id(mint),
        stale=False,
        claimed_name=basics.get("name"),
        claimed_symbol=basics.get("symbol"),
        ratification=standards({"venue": "pool"}, basics.get("controls"), None, False, now),
        observed_at=basics.get("checked_at"),
        source_url="https://solscan.io/token/" + mint,
    )
    return {
        "coin": coin,
        "status": "pending",
        "can_call": False,
        "lookup_pending": True,
        "lookup_invalid": basics.get("status") == "invalid",
        "collected_at": basics.get("checked_at"),
        "history": [],
        "states": [],
        "calls": [],
        "pool_state": None,
    }
