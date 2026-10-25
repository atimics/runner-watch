from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from runner_trading.config import Policy, account_id, stamp
from runner_web.db import connection


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def ensure_account(mode: str, policy: Policy, at: datetime) -> dict[str, Any]:
    identifier = account_id(mode)
    with connection() as db:
        db.execute(
            """INSERT INTO trading_accounts(id,mode,policy_json,policy_hash,cash_usdc,
               sol_lamports,peak_equity_usdc,day,day_equity_usdc,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
            (
                identifier,
                mode,
                policy.json(),
                policy.fingerprint,
                policy.initial_usdc,
                policy.initial_sol,
                policy.initial_usdc,
                at.date().isoformat(),
                policy.initial_usdc,
                stamp(at),
            ),
        )
        db.execute(
            """INSERT INTO trading_ledger(id,account_id,kind,cash_delta_usdc,
               sol_delta_lamports,gas_usdc,recorded_at,receipt_json)
               VALUES(?,?,'capital',?,?,0,?,?) ON CONFLICT(id) DO NOTHING""",
            (
                key(identifier, "capital"),
                identifier,
                policy.initial_usdc,
                policy.initial_sol,
                stamp(at),
                encode({"mode": mode, "policy": policy.fingerprint}),
            ),
        )
        row = dict(
            db.execute("SELECT * FROM trading_accounts WHERE id=?", (identifier,)).fetchone()
        )
    if row["policy_hash"] != policy.fingerprint:
        raise ValueError("The saved account policy must match TRADING_POLICY_JSON")
    return row


def acquire(identifier: str, at: datetime) -> str | None:
    token = secrets.token_hex(24)
    with connection() as db:
        result = db.execute(
            """UPDATE trading_accounts SET lease_token=?,lease_until=?
               WHERE id=? AND (lease_until IS NULL OR lease_until<?)""",
            (token, stamp(at + timedelta(minutes=3)), identifier, stamp(at)),
        )
    return token if result.rowcount == 1 else None


@contextmanager
def locked(identifier: str, token: str, at: datetime):
    with connection() as db:
        changed = db.execute(
            """UPDATE trading_accounts SET lease_until=?
               WHERE id=? AND lease_token=? AND lease_until>=?""",
            (stamp(at + timedelta(minutes=3)), identifier, token, stamp(at)),
        )
        if changed.rowcount != 1:
            raise RuntimeError("Trading lease expired")
        yield db


def release(identifier: str, token: str) -> None:
    with connection() as db:
        db.execute(
            "UPDATE trading_accounts SET lease_token=NULL,lease_until=NULL "
            "WHERE id=? AND lease_token=?",
            (identifier, token),
        )


def rows(table: str, identifier: str, *, active: bool = False) -> list[dict[str, Any]]:
    if table not in {"trading_orders", "trading_positions", "trading_signals"}:
        raise ValueError("Unknown trading table")
    where = ""
    if active:
        where = (
            " AND status='open'"
            if table == "trading_positions"
            else (" AND status IN ('queued','prepared','submitted','checking')")
        )
    with connection() as db:
        return [
            dict(r)
            for r in db.execute(
                f"SELECT * FROM {table} WHERE account_id=?{where}", (identifier,)
            ).fetchall()
        ]


def account(identifier: str) -> dict[str, Any]:
    with connection() as db:
        row = db.execute("SELECT * FROM trading_accounts WHERE id=?", (identifier,)).fetchone()
    if row is None:
        raise LookupError("Trading account is preparing")
    return dict(row)


def event(db: Any, identifier: str, kind: str, detail: dict[str, Any], at: datetime) -> None:
    db.execute(
        "INSERT INTO trading_events VALUES(?,?,?,?,?)",
        (secrets.token_hex(16), identifier, kind, encode(detail), stamp(at)),
    )


def pause(identifier: str, reason: str, at: datetime, *, paused: bool = True) -> None:
    with connection() as db:
        db.execute(
            "UPDATE trading_accounts SET paused=?,pause_reason=? WHERE id=?",
            (int(paused), reason, identifier),
        )
        event(db, identifier, "pause" if paused else "resume", {"reason": reason}, at)


def reserve_buy(
    identifier: str, token: str, signal: dict[str, Any], policy: Policy, at: datetime
) -> str | None:
    order_id = key(signal["id"], "buy")
    with locked(identifier, token, at) as db:
        acc = dict(
            db.execute("SELECT * FROM trading_accounts WHERE id=?", (identifier,)).fetchone()
        )
        positions = db.execute(
            "SELECT COUNT(*) AS n,COALESCE(SUM(cost_usdc),0) AS cost "
            "FROM trading_positions WHERE account_id=? AND status='open'",
            (identifier,),
        ).fetchone()
        pending = db.execute(
            "SELECT COUNT(*) AS n FROM trading_orders WHERE account_id=? "
            "AND side='buy' AND status IN ('queued','prepared','submitted','checking')",
            (identifier,),
        ).fetchone()["n"]
        if (
            acc["paused"]
            or positions["n"] + pending >= policy.max_positions
            or acc["cash_usdc"] - acc["reserved_usdc"] < policy.entry_usdc
            or positions["cost"] + acc["reserved_usdc"] + policy.entry_usdc
            > policy.max_exposure_usdc
            or acc["sol_lamports"]
            < policy.min_sol_lamports + (pending + 1) * policy.max_fee_lamports
        ):
            return None
        exists = db.execute(
            "SELECT 1 FROM trading_positions WHERE account_id=? AND mint=? AND status='open'",
            (identifier, signal["mint"]),
        ).fetchone()
        if exists:
            return None
        inserted = db.execute(
            """INSERT INTO trading_orders(id,account_id,signal_id,mint,side,status,input_raw,
               reserved_usdc,reason,created_at,updated_at,ready_at)
               VALUES(?,?,?,?,'buy','queued',?,?,'Fresh SETUP with checked risk',?,?,?)
               ON CONFLICT DO NOTHING""",
            (
                order_id,
                identifier,
                signal["id"],
                signal["mint"],
                str(policy.entry_usdc),
                policy.entry_usdc,
                stamp(at),
                stamp(at),
                stamp(at + timedelta(seconds=policy.paper_delay_seconds)),
            ),
        )
        if inserted.rowcount != 1:
            return None
        db.execute(
            "UPDATE trading_accounts SET reserved_usdc=reserved_usdc+? WHERE id=?",
            (policy.entry_usdc, identifier),
        )
        db.execute(
            "UPDATE trading_signals SET state='ordered',reason='Entry queued' WHERE id=?",
            (signal["id"],),
        )
        event(db, identifier, "order", {"order_id": order_id, "side": "buy"}, at)
    return order_id


def queue_exit(
    identifier: str, token: str, position: dict[str, Any], reason: str, at: datetime
) -> str | None:
    order_id = secrets.token_hex(16)
    with locked(identifier, token, at) as db:
        current = db.execute(
            "SELECT * FROM trading_positions WHERE id=? AND account_id=? AND status='open'",
            (position["id"], identifier),
        ).fetchone()
        if current is None:
            return None
        inserted = db.execute(
            """INSERT INTO trading_orders(id,account_id,position_id,mint,side,status,input_raw,
               reason,created_at,updated_at,ready_at)
               VALUES(?,?,?,?,'sell','queued',?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (
                order_id,
                identifier,
                position["id"],
                position["mint"],
                current["quantity_raw"],
                reason,
                stamp(at),
                stamp(at),
                stamp(at),
            ),
        )
        if inserted.rowcount != 1:
            return None
        event(db, identifier, "order", {"order_id": order_id, "side": "sell", "reason": reason}, at)
    return order_id


def fail_order(
    identifier: str,
    token: str,
    order_id: str,
    reason: str,
    at: datetime,
    *,
    fee_lamports: int = 0,
    gas_usdc: int = 0,
    receipt: dict | None = None,
) -> None:
    if min(fee_lamports, gas_usdc) < 0:
        raise ValueError("Fees must be nonnegative")
    with locked(identifier, token, at) as db:
        order = db.execute(
            "SELECT * FROM trading_orders WHERE id=? AND account_id=?", (order_id, identifier)
        ).fetchone()
        if order is None or order["status"] in {"filled", "failed", "cancelled"}:
            return
        db.execute(
            "UPDATE trading_orders SET status='failed',reason=?,updated_at=?,result_json=? "
            "WHERE id=?",
            (reason, stamp(at), encode(receipt or {}), order_id),
        )
        db.execute(
            "UPDATE trading_accounts SET reserved_usdc=reserved_usdc-?,"
            "sol_lamports=sol_lamports-?,gas_usdc=gas_usdc+? WHERE id=?",
            (order["reserved_usdc"], fee_lamports, gas_usdc, identifier),
        )
        db.execute(
            """INSERT INTO trading_ledger(id,account_id,order_id,kind,cash_delta_usdc,
                   sol_delta_lamports,gas_usdc,recorded_at,receipt_json)
                   VALUES(?,?,?,'failed',0,?,?,?,?)""",
            (
                key(order_id, "settled"),
                identifier,
                order_id,
                -fee_lamports,
                gas_usdc,
                stamp(at),
                encode(receipt or {"reason": reason}),
            ),
        )


def fill(
    identifier: str,
    token: str,
    order_id: str,
    *,
    input_raw: int,
    output_raw: int,
    fee_lamports: int,
    gas_usdc: int,
    receipt: dict[str, Any],
    at: datetime,
) -> None:
    if min(input_raw, output_raw) <= 0 or min(fee_lamports, gas_usdc) < 0:
        raise ValueError("Fill amounts must be positive and fees nonnegative")
    with locked(identifier, token, at) as db:
        order = db.execute(
            "SELECT * FROM trading_orders WHERE id=? AND account_id=?", (order_id, identifier)
        ).fetchone()
        if order is None:
            raise LookupError("Order missing")
        if order["status"] == "filled":
            return
        if order["status"] not in {"queued", "prepared", "submitted", "checking"}:
            raise ValueError("Order has already resolved")
        if input_raw != int(order["input_raw"]):
            raise ValueError("Fill input differs from the reserved order")
        cash_delta = -input_raw if order["side"] == "buy" else output_raw
        realized = 0
        if order["side"] == "buy":
            acc = db.execute(
                "SELECT mode,paused FROM trading_accounts WHERE id=?", (identifier,)
            ).fetchone()
            if acc["mode"] == "paper" and acc["paused"]:
                raise ValueError("Entries paused")
            signal = db.execute(
                "SELECT coin_id FROM trading_signals WHERE id=?", (order["signal_id"],)
            ).fetchone()
            db.execute(
                """INSERT INTO trading_positions(id,account_id,signal_id,mint,coin_id,status,
                       quantity_raw,cost_usdc,entry_fee_usdc,opened_at)
                       VALUES(?,?,?,?,?,'open',?,?,?,?)""",
                (
                    order_id,
                    identifier,
                    order["signal_id"],
                    order["mint"],
                    signal["coin_id"],
                    str(output_raw),
                    input_raw,
                    gas_usdc,
                    stamp(at),
                ),
            )
        else:
            pos = db.execute(
                "SELECT * FROM trading_positions WHERE id=? AND status='open'",
                (order["position_id"],),
            ).fetchone()
            if pos is None or int(pos["quantity_raw"]) != input_raw:
                raise ValueError("Position quantity differs from the exit")
            realized = output_raw - pos["cost_usdc"] - pos["entry_fee_usdc"] - gas_usdc
            db.execute(
                "UPDATE trading_positions SET status='closed',closed_at=?,exit_usdc=?,"
                "pnl_usdc=?,mark_state='closed' WHERE id=?",
                (stamp(at), output_raw, realized, pos["id"]),
            )
        db.execute(
            """UPDATE trading_accounts SET cash_usdc=cash_usdc+?,
                   reserved_usdc=reserved_usdc-?,sol_lamports=sol_lamports-?,
                   gas_usdc=gas_usdc+?,realized_usdc=realized_usdc+? WHERE id=?""",
            (cash_delta, order["reserved_usdc"], fee_lamports, gas_usdc, realized, identifier),
        )
        db.execute(
            "UPDATE trading_orders SET status='filled',updated_at=?,result_json=? WHERE id=?",
            (stamp(at), encode(receipt), order_id),
        )
        db.execute(
            """INSERT INTO trading_ledger(id,account_id,order_id,kind,cash_delta_usdc,
                   sol_delta_lamports,gas_usdc,mint,quantity_delta_raw,recorded_at,receipt_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key(order_id, "settled"),
                identifier,
                order_id,
                order["side"],
                cash_delta,
                -fee_lamports,
                gas_usdc,
                order["mint"],
                str(output_raw if order["side"] == "buy" else -input_raw),
                stamp(at),
                encode(receipt),
            ),
        )


def save_mark(
    identifier: str, token: str, position_id: str, value: int | None, receipt: dict, at: datetime
) -> None:
    state = "quoted" if value is not None else "exit_unavailable"
    with locked(identifier, token, at) as db:
        db.execute(
            "UPDATE trading_positions SET mark_usdc=?,mark_at=?,mark_state=?,mark_json=? "
            "WHERE id=? AND account_id=? AND status='open'",
            (value, stamp(at), state, encode(receipt), position_id, identifier),
        )
        db.execute(
            "INSERT INTO trading_marks VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
            (position_id, stamp(at), state, value, encode(receipt)),
        )
