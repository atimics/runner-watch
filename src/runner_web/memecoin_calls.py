"""Paper memecoin Calls: staked, and filled at the next quote.

Opening a Call escrows MEMECOIN_CALL_STAKE Flash and records an order. The
first quote observed after the request fills it, so a caller cannot enter or
exit at a quote they have already watched move on the live market. Closing
works the same way. Settlement returns the stake with the return clamped to
[-stake, CALL_WIN_FLASH_CAP], so losses cost Flash and a spread of Calls
closed only when they win no longer pays.

Calls opened before staking (stake 0) keep their original reward: a winning
return pays, a losing one costs nothing.
"""

from __future__ import annotations

import json
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web import memecoins
from runner_web.caller_ids import ensure_caller_identity_with_database
from runner_web.db import connection
from runner_web.flash_wallet import (
    MEMECOIN_CALL_STAKE,
    credit_flash,
    memecoin_call_reward,
    memecoin_call_settlement,
    spend_flash,
)

# An order with no quote after this long is cancelled; an open order's stake
# is refunded. A coin that left the board may never be quoted again.
MEMECOIN_CALL_ORDER_TIMEOUT = timedelta(hours=1)
MEMECOIN_CALL_MAX_AGE_DAYS = 7


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_quoted(coin_id: str) -> dict[str, Any]:
    """The coin exists and has a current quote. The Call fills at a later one."""

    detail = memecoins.memecoin_detail(coin_id)
    if detail is None:
        raise LookupError("Coin not found")
    coin = detail["coin"]
    if detail["status"] != "ok" or coin["stale"]:
        raise ValueError("A source quote from the last 15 minutes is required for a paper Call.")
    price = float(coin["price"])
    if not math.isfinite(price) or price <= 0:
        raise ValueError("A current source quote is required for a paper Call.")
    if (memecoins.pool_state(coin) or {}).get("calls_closed"):
        raise ValueError(
            "This pool holds under "
            f"${memecoins.MIN_CALL_LIQUIDITY_USD:,}, too thin to fill a Call fairly."
        )
    return coin


def _settlement(stake: int, return_pct: float | None, legacy_reward: int) -> int:
    if stake > 0:
        return memecoin_call_settlement(return_pct, stake)
    return legacy_reward


def _call(row: Any, mark: dict[str, Any] | None = None) -> dict[str, Any]:
    saved = dict(row)
    item = {
        key: saved.get(key)
        for key in (
            "public_id",
            "coin_id",
            "symbol",
            "name",
            "caller_handle",
            "status",
            "entry_price",
            "entry_at",
            "exit_price",
            "exit_at",
            "created_at",
            "updated_at",
        )
    }
    stake = int(saved.get("stake") or 0)
    item["stake"] = stake
    price = (
        item["exit_price"]
        if item["status"] == "closed"
        else (mark.get("price") if mark and not mark.get("stale", True) else None)
    )
    item["mark_price"] = price
    item["mark_at"] = (
        item["exit_at"]
        if item["status"] == "closed"
        else (mark.get("observed_at") if price is not None and mark else None)
    )
    change = (float(price) / float(item["entry_price"]) - 1) * 100 if price is not None else None
    item["return_pct"] = round(change, 2) if change is not None and math.isfinite(change) else None
    for field in ("entry_price", "exit_price", "mark_price"):
        item[f"{field}_label"] = (
            memecoins._price_label(float(item[field])) if item[field] is not None else "—"
        )
    item["detail_url"] = f"/memecoins/coin/{item['coin_id']}"
    item["entry_evidence"] = json.loads(saved["entry_evidence"])
    item["exit_evidence"] = json.loads(saved["exit_evidence"]) if saved["exit_evidence"] else None
    if stake > 0:
        flash = int(saved.get("settled_flash") or 0)
    else:
        flash = int(saved.get("flash_reward") or 0)
    item["flash_reward"] = flash
    item["projected_flash_reward"] = (
        _settlement(stake, item["return_pct"], memecoin_call_reward(item["return_pct"]))
        if item["status"] == "active"
        else flash
    )
    if item["status"] != "closed":
        item["reward_label"] = None
    elif flash > 0:
        item["reward_label"] = f"+{flash} Flash"
    elif flash < 0:
        item["reward_label"] = f"−{-flash} Flash"
    else:
        item["reward_label"] = None
    item["closing"] = bool(saved.get("closing"))
    return item


_SELECT = """
    SELECT c.*,ci.handle AS caller_handle,
           COALESCE(ft.amount,0) AS flash_reward,
           q.quote_json AS current_quote_json,q.collected_at AS current_collected_at,
           EXISTS(SELECT 1 FROM memecoin_call_orders o
                  WHERE o.call_public_id=c.public_id AND o.kind='close'
                    AND o.status='pending') AS closing
    FROM memecoin_calls c
    JOIN caller_identities ci ON ci.id=c.caller_identity_id AND ci.status='active'
    LEFT JOIN flash_transactions ft
      ON ft.user_id=c.user_id AND ft.kind='memecoin_call_win' AND ft.reference_id=c.public_id
    LEFT JOIN memecoin_assets q ON q.coin_id=c.coin_id
"""


def _order(row: Any, handle: str) -> dict[str, Any]:
    saved = dict(row)
    return {
        "order_id": saved["order_id"],
        "coin_id": saved["coin_id"],
        "kind": saved["kind"],
        "status": saved["status"],
        "stake": int(saved["stake"] or 0),
        "requested_at": saved["requested_at"],
        "call_public_id": saved.get("call_public_id"),
        "caller_handle": handle,
        "detail_url": f"/memecoins/coin/{saved['coin_id']}",
    }


def create_memecoin_call(
    user_id: str, coin_id: str, *, expected_price: float | None = None
) -> dict[str, Any]:
    """Escrow the stake and queue an open order. `expected_price` is accepted
    for compatibility and ignored: the Call fills at the next quote."""

    del expected_price
    _require_quoted(coin_id)
    order_id = secrets.token_urlsafe(12)
    timestamp = _now()
    with connection() as database:
        identity = ensure_caller_identity_with_database(database, user_id)
        if database.execute(
            "SELECT 1 FROM memecoin_calls WHERE user_id=? AND coin_id=? AND status='active'",
            (user_id, coin_id),
        ).fetchone():
            raise ValueError("You already have an open Call on this coin.")
        inserted = database.execute(
            """
            INSERT INTO memecoin_call_orders(
                order_id,user_id,caller_identity_id,coin_id,kind,stake,status,
                requested_at,created_at,updated_at
            ) VALUES(?,?,?,?,'open',?,'pending',?,?,?) ON CONFLICT DO NOTHING
            """,
            (
                order_id,
                user_id,
                identity["id"],
                coin_id,
                MEMECOIN_CALL_STAKE,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        if inserted.rowcount != 1:
            raise ValueError("A Call on this coin is already waiting for the next quote.")
        # Raises InsufficientFlashError (a ValueError) and rolls the order back.
        spend_flash(
            database,
            user_id,
            MEMECOIN_CALL_STAKE,
            kind="memecoin_call_stake",
            reference_id=order_id,
        )
        row = database.execute(
            "SELECT * FROM memecoin_call_orders WHERE order_id=?", (order_id,)
        ).fetchone()
    return _order(row, str(identity["handle"]))


def close_memecoin_call(
    user_id: str, public_id: str, *, expected_price: float | None = None
) -> dict[str, Any] | None:
    """Queue a close order for an open Call; it settles at the next quote.
    `expected_price` is accepted for compatibility and ignored."""

    del expected_price
    with connection() as database:
        existing = database.execute(
            _SELECT + " WHERE c.user_id=? AND c.public_id=?", (user_id, public_id)
        ).fetchone()
        if existing is None:
            return None
        if existing["status"] == "active" and not existing["closing"]:
            timestamp = _now()
            database.execute(
                """
                INSERT INTO memecoin_call_orders(
                    order_id,user_id,caller_identity_id,coin_id,kind,call_public_id,
                    stake,status,requested_at,created_at,updated_at
                ) VALUES(?,?,?,?,'close',?,0,'pending',?,?,?) ON CONFLICT DO NOTHING
                """,
                (
                    secrets.token_urlsafe(12),
                    user_id,
                    existing["caller_identity_id"],
                    existing["coin_id"],
                    public_id,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            existing = database.execute(
                _SELECT + " WHERE c.user_id=? AND c.public_id=?", (user_id, public_id)
            ).fetchone()
    return _call(existing)


def _next_quote(database: Any, coin_id: str, after: str) -> Any:
    return database.execute(
        """
        SELECT observed_at,price,collected_at,run_id FROM memecoin_quote_history
        WHERE coin_id=? AND observed_at>? AND price>0
        ORDER BY observed_at ASC LIMIT 1
        """,
        (coin_id, after),
    ).fetchone()


def _evidence(coin_id: str, quote: Any, **extra: Any) -> str:
    return json.dumps(
        {
            "coin_id": coin_id,
            "price": float(quote["price"]),
            "observed_at": str(quote["observed_at"]),
            "collected_at": str(quote["collected_at"]),
            "run_id": str(quote["run_id"]),
            **extra,
        },
        allow_nan=False,
    )


def _settle(database: Any, call: Any, quote: Any, *, at: str, **evidence: Any) -> bool:
    """Close an active Call at `quote` and pay its settlement. Returns whether
    this call closed it (a concurrent settle loses the race harmlessly)."""

    stake = int(call["stake"] or 0)
    return_pct = (float(quote["price"]) / float(call["entry_price"]) - 1) * 100
    net = _settlement(stake, return_pct, memecoin_call_reward(return_pct))
    changed = database.execute(
        """
        UPDATE memecoin_calls
        SET status='closed',exit_price=?,exit_at=?,exit_evidence=?,settled_flash=?,updated_at=?
        WHERE public_id=? AND status='active'
        """,
        (
            float(quote["price"]),
            str(quote["observed_at"]),
            _evidence(str(call["coin_id"]), quote, **evidence),
            net,
            at,
            str(call["public_id"]),
        ),
    )
    if changed.rowcount != 1:
        return False
    if stake > 0:
        payout = stake + net
        kind = "memecoin_call_settle"
    else:
        payout = net
        kind = "memecoin_call_win"
    if payout > 0:
        credit_flash(
            database,
            str(call["user_id"]),
            payout,
            kind=kind,
            reference_id=str(call["public_id"]),
        )
    database.execute(
        """
        UPDATE memecoin_call_orders SET status='cancelled',resolved_at=?,updated_at=?
        WHERE call_public_id=? AND status='pending'
        """,
        (at, at, str(call["public_id"])),
    )
    return True


def fill_memecoin_call_orders(at: datetime | None = None) -> list[str]:
    """Fill pending orders at the first quote observed after each request.

    Returns the caller handles whose public records changed.
    """

    current = at or datetime.now(UTC)
    stamp = current.isoformat()
    timeout = (current - MEMECOIN_CALL_ORDER_TIMEOUT).isoformat()
    handles: list[str] = []
    with connection() as database:
        orders = database.execute(
            """
            SELECT o.*,ci.handle AS caller_handle FROM memecoin_call_orders o
            JOIN caller_identities ci ON ci.id=o.caller_identity_id
            WHERE o.status='pending' ORDER BY o.requested_at,o.order_id
            """
        ).fetchall()
        for order in orders:
            quote = _next_quote(database, str(order["coin_id"]), str(order["requested_at"]))
            resolved = False
            if quote is None:
                if str(order["requested_at"]) >= timeout:
                    continue
                if order["kind"] == "open" and int(order["stake"] or 0) > 0:
                    credit_flash(
                        database,
                        str(order["user_id"]),
                        int(order["stake"]),
                        kind="memecoin_call_refund",
                        reference_id=str(order["order_id"]),
                    )
                status = "cancelled"
            elif order["kind"] == "open":
                asset = database.execute(
                    "SELECT quote_json FROM memecoin_assets WHERE coin_id=?",
                    (str(order["coin_id"]),),
                ).fetchone()
                shown = json.loads(asset["quote_json"]) if asset else {}
                opened = database.execute(
                    """
                    INSERT INTO memecoin_calls(
                        public_id,user_id,caller_identity_id,coin_id,symbol,name,status,
                        entry_price,entry_at,entry_evidence,stake,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,'active',?,?,?,?,?,?) ON CONFLICT DO NOTHING
                    """,
                    (
                        secrets.token_urlsafe(12),
                        str(order["user_id"]),
                        str(order["caller_identity_id"]),
                        str(order["coin_id"]),
                        str(shown.get("symbol") or order["coin_id"])[:64],
                        str(shown.get("name") or order["coin_id"])[:160],
                        float(quote["price"]),
                        str(quote["observed_at"]),
                        _evidence(
                            str(order["coin_id"]),
                            quote,
                            order_id=str(order["order_id"]),
                            requested_at=str(order["requested_at"]),
                        ),
                        int(order["stake"] or 0),
                        stamp,
                        stamp,
                    ),
                )
                if opened.rowcount == 1:
                    status, resolved = "filled", True
                else:
                    credit_flash(
                        database,
                        str(order["user_id"]),
                        int(order["stake"] or 0),
                        kind="memecoin_call_refund",
                        reference_id=str(order["order_id"]),
                    )
                    status = "cancelled"
            else:
                call = database.execute(
                    "SELECT * FROM memecoin_calls WHERE public_id=? AND status='active'",
                    (str(order["call_public_id"]),),
                ).fetchone()
                if call is not None and str(quote["observed_at"]) >= str(call["entry_at"]):
                    resolved = _settle(
                        database,
                        call,
                        quote,
                        at=stamp,
                        order_id=str(order["order_id"]),
                        requested_at=str(order["requested_at"]),
                    )
                status = "filled" if resolved else "cancelled"
            database.execute(
                """
                UPDATE memecoin_call_orders SET status=?,resolved_at=?,updated_at=?
                WHERE order_id=? AND status='pending'
                """,
                (status, stamp, stamp, str(order["order_id"])),
            )
            handles.append(str(order["caller_handle"]))
    return handles


def pending_memecoin_order(user_id: str, coin_id: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """
            SELECT o.*,ci.handle AS caller_handle FROM memecoin_call_orders o
            JOIN caller_identities ci ON ci.id=o.caller_identity_id
            WHERE o.user_id=? AND o.coin_id=? AND o.status='pending'
            """,
            (user_id, coin_id),
        ).fetchone()
    return _order(row, str(row["caller_handle"])) if row else None


def memecoin_calls(
    *,
    coin_id: str | None = None,
    caller_handle: str | None = None,
    user_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses, parameters = [], []
    for field, value in (
        ("c.coin_id", coin_id),
        ("ci.handle", caller_handle),
        ("c.user_id", user_id),
    ):
        if value is not None:
            clauses.append(f"{field}=?")
            parameters.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connection() as database:
        rows = database.execute(
            _SELECT + where + " ORDER BY c.updated_at DESC,c.public_id DESC LIMIT ?",
            (*parameters, max(1, min(limit, 500))),
        ).fetchall()
    current = datetime.now(UTC)
    enabled = memecoins.memecoins_enabled()
    return [
        _call(
            row,
            memecoins._quote_display(
                json.loads(row["current_quote_json"]), row["current_collected_at"], current
            )
            if enabled and row["current_quote_json"]
            else None,
        )
        for row in rows
    ]


def active_memecoin_call(user_id: str, coin_id: str) -> dict[str, Any] | None:
    return next(
        (
            call
            for call in memecoin_calls(user_id=user_id, coin_id=coin_id)
            if call["status"] == "active"
        ),
        None,
    )


def expire_memecoin_calls(
    at: datetime | None = None,
    *,
    max_age_days: int = MEMECOIN_CALL_MAX_AGE_DAYS,
) -> list[str]:
    """Settle memecoin Calls left open past the expiry window at the latest stored quote.

    Returns the caller handles whose public records changed. A Call with no
    stored quote at or after its entry time stays open until a quote arrives.
    """
    current = at or datetime.now(UTC)
    cutoff = (current - timedelta(days=max_age_days)).isoformat()
    handles: list[str] = []
    with connection() as database:
        rows = database.execute(
            """
            SELECT c.*,ci.handle AS caller_handle
            FROM memecoin_calls c
            JOIN caller_identities ci ON ci.id=c.caller_identity_id
            WHERE c.status='active' AND c.entry_at<?
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            quote = database.execute(
                """
                SELECT observed_at,price,collected_at,run_id FROM memecoin_quote_history
                WHERE coin_id=? AND observed_at>=? AND price>0
                ORDER BY observed_at DESC LIMIT 1
                """,
                (str(row["coin_id"]), str(row["entry_at"])),
            ).fetchone()
            if quote is None:
                continue
            if _settle(database, row, quote, at=current.isoformat(), auto_expired=True):
                handles.append(str(row["caller_handle"]))
    return handles
