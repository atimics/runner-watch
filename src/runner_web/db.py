from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/runner-watch.db"))


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def init_db() -> None:
    with connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passkeys (
                credential_id BLOB PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                public_key BLOB NOT NULL,
                sign_count INTEGER NOT NULL DEFAULT 0,
                device_type TEXT,
                backed_up INTEGER NOT NULL DEFAULT 0,
                transports TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                last_used_at TEXT
            );
            CREATE TABLE IF NOT EXISTS auth_challenges (
                token TEXT PRIMARY KEY,
                user_id TEXT REFERENCES users(id),
                kind TEXT NOT NULL,
                challenge BLOB NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scan_snapshots (
                id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                score REAL NOT NULL,
                stage TEXT NOT NULL,
                session TEXT NOT NULL,
                price REAL NOT NULL,
                change_pct REAL NOT NULL,
                momentum_5m_pct REAL NOT NULL,
                momentum_15m_pct REAL NOT NULL,
                relative_volume REAL,
                recent_relative_volume REAL,
                breakout_pct REAL NOT NULL,
                dollar_volume REAL NOT NULL,
                quote_time TEXT NOT NULL,
                signals_json TEXT NOT NULL,
                risks_json TEXT NOT NULL,
                captured_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scan_snapshots_captured
                ON scan_snapshots(captured_at DESC);
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                public_id TEXT UNIQUE NOT NULL,
                snapshot_id TEXT NOT NULL REFERENCES scan_snapshots(id),
                user_id TEXT NOT NULL REFERENCES users(id),
                thesis TEXT NOT NULL,
                horizon TEXT NOT NULL,
                invalidation TEXT NOT NULL,
                disclosure TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'public',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS signals_created ON signals(created_at DESC);
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                signal_id TEXT NOT NULL REFERENCES signals(id),
                user_id TEXT NOT NULL REFERENCES users(id),
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(signal_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS watches (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                ticker TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(user_id, ticker)
            );
            CREATE INDEX IF NOT EXISTS watches_ticker ON watches(ticker);
            CREATE TABLE IF NOT EXISTS sec_companies (
                cik INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                name TEXT NOT NULL,
                exchange TEXT NOT NULL,
                refreshed_at TEXT NOT NULL,
                PRIMARY KEY(cik, ticker)
            );
            CREATE INDEX IF NOT EXISTS sec_companies_ticker ON sec_companies(ticker);
            CREATE TABLE IF NOT EXISTS sec_filings (
                accession TEXT PRIMARY KEY,
                cik INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                company TEXT NOT NULL,
                form TEXT NOT NULL,
                kind TEXT NOT NULL,
                sentiment TEXT NOT NULL,
                score REAL NOT NULL,
                title TEXT NOT NULL,
                filed_at TEXT NOT NULL,
                filing_url TEXT NOT NULL,
                actor TEXT,
                actor_title TEXT,
                transaction_codes TEXT NOT NULL DEFAULT '',
                transaction_shares REAL,
                transaction_price REAL,
                transaction_value REAL,
                price REAL,
                change_pct REAL,
                relative_volume REAL,
                market_score REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sec_filings_filed ON sec_filings(filed_at DESC);
            CREATE INDEX IF NOT EXISTS sec_filings_score ON sec_filings(score DESC);
            CREATE INDEX IF NOT EXISTS sec_filings_ticker ON sec_filings(ticker, filed_at DESC);
            CREATE TABLE IF NOT EXISTS sec_outcomes (
                accession TEXT PRIMARY KEY REFERENCES sec_filings(accession) ON DELETE CASCADE,
                base_price REAL NOT NULL,
                base_at TEXT NOT NULL,
                price_1h REAL,
                return_1h_pct REAL,
                observed_1h_at TEXT,
                price_1d REAL,
                return_1d_pct REAL,
                observed_1d_at TEXT,
                price_5d REAL,
                return_5d_pct REAL,
                observed_5d_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sec_outcomes_base_at ON sec_outcomes(base_at DESC);
            CREATE TABLE IF NOT EXISTS worker_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
