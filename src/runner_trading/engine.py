from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_trading import ledger
from runner_trading.config import USDC, Policy, age, stamp
from runner_trading.providers import Jupiter, ProviderError
from runner_trading.strategy import entry_reasons, exit_reason
from runner_web.db import connection


def saved_rows() -> list[dict[str, Any]]:
    with connection() as db:
        row = db.execute("SELECT value FROM worker_state WHERE key='memecoins_snapshot'").fetchone()
    return (json.loads(row["value"]).get("rows") or []) if row else []


class Trader:
    """A leased account worker. Every external fill has one durable order."""

    def __init__(
        self,
        policy: Policy,
        *,
        mode: str = "paper",
        provider=None,
        live=None,
        clock=None,
        source=saved_rows,
    ):
        if mode not in {"paper", "live"} or (mode == "live" and live is None):
            raise ValueError("Live mode requires a configured execution adapter")
        self.policy = policy
        self.mode = mode
        self.clock = clock or (lambda: datetime.now(UTC))
        self.provider = provider or Jupiter(policy, clock=self.clock)
        self.live = live
        self.source = source
        self.identifier = ledger.ensure_account(mode, policy, self.clock())["id"]
        self.token = ""

    def cycle(self) -> dict:
        token = ledger.acquire(self.identifier, self.clock())
        if not token:
            return {"state": "leased"}
        self.token = token
        errors = []
        try:
            board = self.source()
            by_mint = {r["token_address"]: r for r in board if r.get("token_address")}
            # A pending signature always gets a chain check before a new order.
            self.orders(by_mint, recover_only=True)
            if self.live:
                self.live.reconcile(self.identifier, self.token, self.clock())
            self.monitor(by_mint)
            self.risk_limits()
            self.orders(by_mint)
            self.signals(board)
            self.outcomes()
        except (ProviderError, ValueError, LookupError) as exc:
            errors.append(str(exc))
        finally:
            try:
                at = self.clock()
                positions = ledger.rows("trading_positions", self.identifier, active=True)
                unavailable = sum(
                    p["mark_state"] != "quoted"
                    or age(p["mark_at"], at) > self.policy.cycle_seconds * 3
                    for p in positions
                )
                pending = ledger.rows("trading_orders", self.identifier, active=True)
                uncertain = sum(o["status"] != "queued" for o in pending)
                health = {
                    "state": "degraded" if errors or unavailable or uncertain else "ok",
                    "errors": errors,
                    "positions": len(positions),
                    "unavailable_exits": unavailable,
                    "pending_orders": len(pending),
                    "uncertain_orders": uncertain,
                    "at": stamp(at),
                }
                with ledger.locked(self.identifier, self.token, at) as db:
                    db.execute(
                        "UPDATE trading_accounts SET heartbeat_at=?,health_json=? WHERE id=?",
                        (stamp(at), ledger.encode(health), self.identifier),
                    )
            finally:
                ledger.release(self.identifier, self.token)
        return health

    def monitor(self, board: dict) -> None:
        for pos in ledger.rows("trading_positions", self.identifier, active=True):
            value = None
            try:
                quote = self.provider.quote(pos["mint"], USDC, int(pos["quantity_raw"]))
                gas = self.provider.gas_cost(quote.fee_lamports)
                quote.require_fresh(self.policy, self.clock())
                value = (
                    self.provider.paper_output(quote) if self.mode == "paper" else quote.minimum_raw
                )
                receipt = {**quote.receipt(), "exit_gas_usdc": gas}
                net = max(0, value - gas)
            except (ProviderError, ValueError) as exc:
                receipt = {"reason": str(exc)}
                net = None
            ledger.save_mark(self.identifier, self.token, pos["id"], net, receipt, self.clock())
            reason = exit_reason(pos, board.get(pos["mint"]), net, self.policy, self.clock())
            if reason:
                ledger.queue_exit(self.identifier, self.token, pos, reason, self.clock())

    def risk_limits(self) -> None:
        at = self.clock()
        with ledger.locked(self.identifier, self.token, at) as db:
            acc = dict(
                db.execute(
                    "SELECT * FROM trading_accounts WHERE id=?", (self.identifier,)
                ).fetchone()
            )
            positions = db.execute(
                "SELECT mark_usdc,mark_at FROM trading_positions "
                "WHERE account_id=? AND status='open'",
                (self.identifier,),
            ).fetchall()
            missing = any(
                p["mark_usdc"] is None or age(p["mark_at"], at) > self.policy.cycle_seconds * 3
                for p in positions
            )
            if missing:
                # Hold the last complete equity anchor. An unavailable route gets
                # a separate health state and closes the entry gate.
                return
            equity = acc["cash_usdc"] + sum(p["mark_usdc"] for p in positions) - acc["gas_usdc"]
            day_equity = acc["day_equity_usdc"] if acc["day"] == at.date().isoformat() else equity
            peak = max(equity, acc["peak_equity_usdc"])
            reason = ""
            if day_equity - equity >= self.policy.daily_loss_usdc:
                reason = "Daily loss limit reached"
            if equity * 10_000 <= peak * (10_000 - self.policy.max_drawdown_bps):
                reason = "Account drawdown limit reached"
            db.execute(
                "UPDATE trading_accounts SET day=?,day_equity_usdc=?,peak_equity_usdc=? WHERE id=?",
                (at.date().isoformat(), day_equity, peak, self.identifier),
            )
            if reason and not acc["paused"]:
                db.execute(
                    "UPDATE trading_accounts SET paused=1,pause_reason=? WHERE id=?",
                    (reason, self.identifier),
                )
                ledger.event(db, self.identifier, "risk_pause", {"reason": reason}, at)

    def entry_health(self) -> list[str]:
        reasons = []
        acc = ledger.account(self.identifier)
        if acc["paused"]:
            reasons.append(acc["pause_reason"] or "Entries paused")
        for pos in ledger.rows("trading_positions", self.identifier, active=True):
            if (
                pos["mark_usdc"] is None
                or age(pos["mark_at"], self.clock()) > self.policy.cycle_seconds * 3
            ):
                reasons.append("Refresh every position's exit quote")
                break
        if any(
            o["status"] != "queued"
            for o in ledger.rows("trading_orders", self.identifier, active=True)
        ):
            reasons.append("Resolve the pending chain transaction")
        return reasons

    def signals(self, board: list[dict]) -> None:
        created = []
        with ledger.locked(self.identifier, self.token, self.clock()) as db:
            for row in board:
                mint, observed = row.get("token_address"), row.get("observed_at")
                if not mint or not observed or not row.get("id") or row.get("network") != "solana":
                    continue
                state = (row.get("early") or {}).get("state", "unknown")
                previous = db.execute(
                    "SELECT * FROM trading_seen WHERE account_id=? AND mint=?",
                    (self.identifier, mint),
                ).fetchone()
                if previous and observed <= previous["observed_at"]:
                    continue
                signal_id = None
                if state == "setup" and (previous is None or previous["state"] != "setup"):
                    signal_id = ledger.key(self.identifier, mint, observed)
                    reasons = entry_reasons(row, self.policy, self.clock())
                    db.execute(
                        """INSERT INTO trading_signals(id,account_id,mint,coin_id,observed_at,
                               state,reason,evidence_json,outcome_due_at) VALUES(?,?,?,?,?,?,?,?,?)
                               ON CONFLICT DO NOTHING""",
                        (
                            signal_id,
                            self.identifier,
                            mint,
                            row["id"],
                            observed,
                            "observed",
                            "; ".join(reasons) or "Risk evidence checked",
                            ledger.encode(row),
                            stamp(self.clock() + timedelta(seconds=self.policy.max_hold_seconds)),
                        ),
                    )
                    created.append({"id": signal_id, "mint": mint, "row": row, "reasons": reasons})
                db.execute(
                    """INSERT INTO trading_seen VALUES(?,?,?,?,?) ON CONFLICT(account_id,mint)
                           DO UPDATE SET state=excluded.state,observed_at=excluded.observed_at,
                           signal_id=excluded.signal_id""",
                    (self.identifier, mint, state, observed, signal_id),
                )
        # Resume decisions saved immediately before a process restart.
        with connection() as db:
            waiting = db.execute(
                "SELECT * FROM trading_signals WHERE account_id=? AND state='observed' "
                "ORDER BY observed_at LIMIT 200",
                (self.identifier,),
            ).fetchall()
        created = [
            {
                "id": s["id"],
                "mint": s["mint"],
                "row": json.loads(s["evidence_json"]),
                "reasons": entry_reasons(json.loads(s["evidence_json"]), self.policy, self.clock()),
            }
            for s in waiting
        ]
        for index, signal in enumerate(created):
            reasons = signal["reasons"] + self.entry_health()
            if index >= self.policy.max_new_signals:
                reasons.append("Signal quote budget reached")
            else:
                try:
                    buy = self.provider.quote(USDC, signal["mint"], self.policy.entry_usdc)
                    units = self.provider.paper_output(buy)
                    cost = buy.input_raw + self.provider.gas_cost(buy.fee_lamports)
                    buy.require_fresh(self.policy, self.clock())
                    with ledger.locked(self.identifier, self.token, self.clock()) as db:
                        db.execute(
                            "UPDATE trading_signals SET baseline_units=?,baseline_cost_usdc=? "
                            "WHERE id=?",
                            (str(units), cost, signal["id"]),
                        )
                except (ProviderError, ValueError) as exc:
                    reasons.append(str(exc))
            if not reasons and ledger.reserve_buy(
                self.identifier, self.token, signal, self.policy, self.clock()
            ):
                continue
            with ledger.locked(self.identifier, self.token, self.clock()) as db:
                db.execute(
                    "UPDATE trading_signals SET state='skipped',reason=? WHERE id=?",
                    (
                        "; ".join(reasons) or "Account position, cash or exposure limit",
                        signal["id"],
                    ),
                )

    def orders(self, board: dict, *, recover_only: bool = False) -> None:
        orders = ledger.rows("trading_orders", self.identifier, active=True)
        for order in sorted(orders, key=lambda o: (o["side"] == "buy", o["created_at"])):
            if order["status"] != "queued":
                if self.live:
                    self.live.recover(self.identifier, self.token, order, self.clock())
                continue
            if recover_only or age(order["ready_at"], self.clock()) < 0:
                continue
            if order["side"] == "buy":
                reasons = entry_reasons(board.get(order["mint"], {}), self.policy, self.clock())
                reasons += self.entry_health()
                if age(order["created_at"], self.clock()) > self.policy.entry_timeout_seconds:
                    reasons.append("Entry time window ended")
                if reasons:
                    ledger.fail_order(
                        self.identifier, self.token, order["id"], "; ".join(reasons), self.clock()
                    )
                    continue
            try:
                buying = order["side"] == "buy"
                quote = self.provider.quote(
                    USDC if buying else order["mint"],
                    order["mint"] if buying else USDC,
                    int(order["input_raw"]),
                    **({"taker": self.live.wallet} if self.live else {}),
                )
                output = (
                    self.provider.paper_output(quote) if self.mode == "paper" else quote.minimum_raw
                )
                gas = self.provider.gas_cost(quote.fee_lamports)
                if buying:
                    reverse = self.provider.quote(order["mint"], USDC, output)
                    returned = (
                        self.provider.paper_output(reverse)
                        if self.mode == "paper"
                        else reverse.minimum_raw
                    )
                    net = returned - gas - self.provider.gas_cost(reverse.fee_lamports)
                    if net * 10_000 < quote.input_raw * (
                        10_000 - self.policy.max_round_trip_loss_bps
                    ):
                        raise ProviderError("The size-specific return quote exceeds the cost limit")
                quote.require_fresh(self.policy, self.clock())
                if self.live:
                    self.live.prepare(self.identifier, self.token, order, quote, self.clock())
                else:
                    # Recheck the operator switch after all provider calls.
                    if buying and ledger.account(self.identifier)["paused"]:
                        raise ProviderError("Entries paused")
                    ledger.fill(
                        self.identifier,
                        self.token,
                        order["id"],
                        input_raw=quote.input_raw,
                        output_raw=output,
                        fee_lamports=quote.fee_lamports,
                        gas_usdc=gas,
                        receipt={
                            **quote.receipt(),
                            "mode": "paper",
                            "model": self.policy.paper_model,
                            "filled_output_raw": str(output),
                            "gas_usdc": gas,
                        },
                        at=self.clock(),
                    )
            except (ProviderError, ValueError) as exc:
                if order["side"] == "buy":
                    ledger.fail_order(
                        self.identifier, self.token, order["id"], str(exc), self.clock()
                    )
                else:
                    with ledger.locked(self.identifier, self.token, self.clock()) as db:
                        db.execute(
                            "UPDATE trading_orders SET attempts=attempts+1,updated_at=?,ready_at=?,"
                            "result_json=? WHERE id=? AND status='queued'",
                            (
                                stamp(self.clock()),
                                stamp(self.clock() + timedelta(seconds=60)),
                                ledger.encode({"reason": str(exc), "retry": True}),
                                order["id"],
                            ),
                        )

    def outcomes(self) -> None:
        with connection() as db:
            due = db.execute(
                "SELECT * FROM trading_signals WHERE account_id=? AND outcome_at IS NULL "
                "AND outcome_due_at<=? ORDER BY outcome_due_at LIMIT ?",
                (self.identifier, stamp(self.clock()), self.policy.max_new_signals),
            ).fetchall()
        for row in due:
            result = {"state": "unavailable", "reason": "Entry quote unavailable"}
            if row["baseline_units"]:
                try:
                    quote = self.provider.quote(row["mint"], USDC, int(row["baseline_units"]))
                    value = self.provider.paper_output(quote) - self.provider.gas_cost(
                        quote.fee_lamports
                    )
                    quote.require_fresh(self.policy, self.clock())
                    result = {
                        "state": "quoted",
                        "net_usdc": value,
                        "pnl_usdc": value - row["baseline_cost_usdc"],
                        "quote": quote.receipt(),
                    }
                except (ProviderError, ValueError) as exc:
                    result = {"state": "unavailable", "reason": str(exc)}
            result["late_seconds"] = max(0, int(age(row["outcome_due_at"], self.clock())))
            with ledger.locked(self.identifier, self.token, self.clock()) as db:
                db.execute(
                    "UPDATE trading_signals SET outcome_at=?,outcome_json=? "
                    "WHERE id=? AND outcome_at IS NULL",
                    (stamp(self.clock()), ledger.encode(result), row["id"]),
                )
