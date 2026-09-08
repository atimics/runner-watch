"""Transaction-backed observations about wallets named in pool creation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from runner_web.helius_discovery import PUMP_SWAP, _decode

BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])


def creator_trades(
    entries: list[Any], pools: list[dict[str, Any]], *, at: datetime
) -> list[dict[str, Any]]:
    known = {pool["pool_address"]: pool for pool in pools}
    alerts = {}
    for entry in entries:
        try:
            meta = entry["meta"]
            stamp = entry["blockTime"]
            if (
                meta["err"] is not None
                or type(stamp) is not int
                or not 0 <= at.timestamp() - stamp <= 86400
            ):
                continue
            tx = entry["transaction"]
            signature = tx["signatures"][0]
            if len(_decode(signature, 88)) != 64:
                continue
            instructions = list(tx["message"]["instructions"])
            for group in meta.get("innerInstructions") or []:
                instructions.extend(group["instructions"])
            for ix in instructions:
                try:
                    if ix.get("programId") != PUMP_SWAP:
                        continue
                    discriminator = _decode(ix.get("data"))[:8]
                    if discriminator not in (BUY, SELL):
                        continue
                    accounts = ix["accounts"]
                    pool = known.get(accounts[0])
                    if (
                        pool is None
                        or accounts[3] != pool["token_address"]
                        or type(entry.get("slot")) is not int
                        or entry["slot"] < pool["slot"]
                    ):
                        continue
                    wallet = accounts[1]
                    roles = [
                        role
                        for role in ("pool_creator", "declared_creator")
                        if pool.get(role) == wallet and wallet != "1" * 32
                    ]
                    if not roles:
                        continue
                    # Report the wallet's net token change across this transaction.
                    totals = []
                    for field in ("preTokenBalances", "postTokenBalances"):
                        total = Decimal(0)
                        for balance in meta[field]:
                            if (
                                balance.get("owner") == wallet
                                and balance.get("mint") == accounts[3]
                            ):
                                amount = balance["uiTokenAmount"]
                                decimals = amount["decimals"]
                                raw = amount["amount"]
                                if (
                                    type(decimals) is not int
                                    or not 0 <= decimals <= 255
                                    or not isinstance(raw, str)
                                    or len(raw) > 20
                                    or not raw.isdigit()
                                ):
                                    raise ValueError("Invalid token balance")
                                total += Decimal(raw).scaleb(-decimals)
                        totals.append(total)
                    delta = totals[1] - totals[0]
                    direction = "sell" if discriminator == SELL else "buy"
                    if (direction == "sell" and delta >= 0) or (direction == "buy" and delta <= 0):
                        continue
                    alert_id = f"{signature}:{accounts[0]}:{wallet}:{direction}"
                    alerts[alert_id] = {
                        "id": alert_id,
                        "kind": "creator_" + direction,
                        "title": "Pool-linked wallet "
                        + ("sold" if direction == "sell" else "bought"),
                        "wallet": wallet,
                        "roles": roles,
                        "role_label": " / ".join(
                            "Pool creator" if role == "pool_creator" else "Declared coin creator"
                            for role in roles
                        ),
                        "token_address": accounts[3],
                        "pool_address": accounts[0],
                        "net_token_amount": format(abs(delta), "f"),
                        "observed_at": datetime.fromtimestamp(stamp, UTC).isoformat(),
                        "signature": signature,
                        "slot": entry["slot"],
                        "source_url": f"https://solscan.io/tx/{signature}",
                        "relationship_source_url": pool["source_url"],
                        "relationship": "Wallet named in this pool's creation transaction",
                        "amount_basis": "wallet_net_change",
                        "commitment": "finalized",
                    }
                except (KeyError, IndexError, TypeError, ValueError, InvalidOperation):
                    continue
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            continue
    return sorted(
        alerts.values(), key=lambda alert: (alert["observed_at"], alert["id"]), reverse=True
    )
