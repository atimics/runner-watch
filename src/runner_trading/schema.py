from __future__ import annotations

from typing import Any


def migrate(db: Any) -> None:
    db.executescript("""
        CREATE TABLE trading_accounts (
            id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('paper','live')),
            policy_json TEXT NOT NULL, policy_hash TEXT NOT NULL,
            cash_usdc BIGINT NOT NULL CHECK(cash_usdc>=0),
            reserved_usdc BIGINT NOT NULL DEFAULT 0 CHECK(reserved_usdc>=0),
            sol_lamports BIGINT NOT NULL CHECK(sol_lamports>=0),
            gas_usdc BIGINT NOT NULL DEFAULT 0,
            realized_usdc BIGINT NOT NULL DEFAULT 0,
            peak_equity_usdc BIGINT NOT NULL, day TEXT NOT NULL,
            day_equity_usdc BIGINT NOT NULL,
            paused INTEGER NOT NULL DEFAULT 0, pause_reason TEXT NOT NULL DEFAULT '',
            lease_token TEXT, lease_until TEXT, heartbeat_at TEXT,
            health_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            CHECK(reserved_usdc<=cash_usdc)
        );
        CREATE TABLE trading_signals (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES trading_accounts(id),
            mint TEXT NOT NULL, coin_id TEXT NOT NULL, observed_at TEXT NOT NULL,
            state TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL,
            baseline_units TEXT, baseline_cost_usdc BIGINT,
            outcome_due_at TEXT NOT NULL, outcome_at TEXT, outcome_json TEXT,
            UNIQUE(account_id,mint,observed_at)
        );
        CREATE INDEX trading_signals_due ON trading_signals(account_id,outcome_at,outcome_due_at);
        CREATE TABLE trading_seen (
            account_id TEXT NOT NULL REFERENCES trading_accounts(id), mint TEXT NOT NULL,
            state TEXT NOT NULL, observed_at TEXT NOT NULL, signal_id TEXT,
            PRIMARY KEY(account_id,mint)
        );
        CREATE TABLE trading_orders (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES trading_accounts(id),
            signal_id TEXT REFERENCES trading_signals(id), position_id TEXT,
            mint TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('buy','sell')),
            status TEXT NOT NULL CHECK(status IN
              ('queued','prepared','submitted','checking','filled','failed','cancelled')),
            input_raw TEXT NOT NULL, reserved_usdc BIGINT NOT NULL DEFAULT 0,
            reason TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            ready_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            quote_json TEXT, signature TEXT UNIQUE, signed_transaction TEXT,
            last_valid_height BIGINT, result_json TEXT,
            UNIQUE(signal_id,side)
        );
        CREATE UNIQUE INDEX trading_orders_one_pending ON trading_orders(account_id,mint)
            WHERE status IN ('queued','prepared','submitted','checking');
        CREATE INDEX trading_orders_work ON trading_orders(account_id,status,ready_at);
        CREATE TABLE trading_positions (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES trading_accounts(id),
            signal_id TEXT NOT NULL REFERENCES trading_signals(id), mint TEXT NOT NULL,
            coin_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','closed')),
            quantity_raw TEXT NOT NULL, cost_usdc BIGINT NOT NULL, entry_fee_usdc BIGINT NOT NULL,
            opened_at TEXT NOT NULL, closed_at TEXT, exit_usdc BIGINT, pnl_usdc BIGINT,
            mark_usdc BIGINT, mark_at TEXT, mark_state TEXT NOT NULL DEFAULT 'pending',
            mark_json TEXT, exit_requested INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX trading_positions_one_open ON trading_positions(account_id,mint)
            WHERE status='open';
        CREATE TABLE trading_ledger (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES trading_accounts(id),
            order_id TEXT UNIQUE REFERENCES trading_orders(id), kind TEXT NOT NULL,
            cash_delta_usdc BIGINT NOT NULL, sol_delta_lamports BIGINT NOT NULL,
            gas_usdc BIGINT NOT NULL, mint TEXT, quantity_delta_raw TEXT,
            recorded_at TEXT NOT NULL, receipt_json TEXT NOT NULL
        );
        CREATE TABLE trading_events (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES trading_accounts(id),
            kind TEXT NOT NULL, detail_json TEXT NOT NULL, recorded_at TEXT NOT NULL
        );
        CREATE TABLE trading_marks (
            position_id TEXT NOT NULL REFERENCES trading_positions(id),
            observed_at TEXT NOT NULL, state TEXT NOT NULL, value_usdc BIGINT,
            receipt_json TEXT NOT NULL, PRIMARY KEY(position_id,observed_at)
        );
    """)
    db.execute("ALTER TABLE memecoin_calls ADD COLUMN resolution_state TEXT NOT NULL DEFAULT 'ok'")
    db.execute("ALTER TABLE memecoin_calls ADD COLUMN resolution_reason TEXT")
