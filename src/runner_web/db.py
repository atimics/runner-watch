from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/runner-watch.db"))


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(db: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


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
            CREATE TABLE IF NOT EXISTS ingestion_runs (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                feed TEXT NOT NULL,
                locator TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_count INTEGER NOT NULL DEFAULT 0,
                received_count INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT,
                content_type TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ingestion_runs_source_time
                ON ingestion_runs(source,feed,finished_at DESC);
            CREATE INDEX IF NOT EXISTS ingestion_runs_status_time
                ON ingestion_runs(status,finished_at DESC);
            CREATE TABLE IF NOT EXISTS ingestion_items (
                run_id TEXT NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                PRIMARY KEY(run_id,item_key)
            );
            CREATE INDEX IF NOT EXISTS ingestion_items_key
                ON ingestion_items(item_key,run_id);
            CREATE TABLE IF NOT EXISTS source_item_state (
                source TEXT NOT NULL,
                feed TEXT NOT NULL,
                item_key TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                parser_version TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                processed_at TEXT,
                PRIMARY KEY(source,feed,item_key)
            );
            CREATE INDEX IF NOT EXISTS source_item_state_status
                ON source_item_state(source,feed,status,last_seen_at DESC);
            CREATE TABLE IF NOT EXISTS scan_runs (
                id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                label TEXT NOT NULL,
                feature_schema_version TEXT NOT NULL,
                requested_symbols INTEGER NOT NULL,
                liquid_symbols INTEGER NOT NULL,
                scanned_symbols INTEGER NOT NULL,
                candidate_rows INTEGER NOT NULL,
                failed_symbols_json TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                captured_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scan_runs_captured
                ON scan_runs(captured_at DESC);
            CREATE TABLE IF NOT EXISTS scan_outcomes (
                snapshot_id TEXT PRIMARY KEY REFERENCES scan_snapshots(id) ON DELETE CASCADE,
                ticker TEXT NOT NULL,
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
            CREATE INDEX IF NOT EXISTS scan_outcomes_base_at
                ON scan_outcomes(base_at DESC);
            CREATE TABLE IF NOT EXISTS market_bars (
                source TEXT NOT NULL,
                ticker TEXT NOT NULL,
                interval TEXT NOT NULL,
                bar_time TEXT NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                first_collected_at TEXT NOT NULL,
                last_collected_at TEXT NOT NULL,
                PRIMARY KEY(source,ticker,interval,bar_time)
            );
            CREATE INDEX IF NOT EXISTS market_bars_ticker_time
                ON market_bars(ticker,interval,bar_time DESC);
            CREATE TABLE IF NOT EXISTS source_documents (
                source TEXT NOT NULL,
                source_url TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                content_type TEXT,
                content_encoding TEXT NOT NULL DEFAULT 'identity',
                content BLOB NOT NULL,
                first_collected_at TEXT NOT NULL,
                last_collected_at TEXT NOT NULL,
                PRIMARY KEY(source_url,content_hash)
            );
            CREATE INDEX IF NOT EXISTS source_documents_fetched
                ON source_documents(last_collected_at DESC);
            CREATE TABLE IF NOT EXISTS ranker_models (
                id TEXT PRIMARY KEY,
                feature_schema_version TEXT NOT NULL,
                horizon TEXT NOT NULL,
                model_kind TEXT NOT NULL,
                weights_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                training_start TEXT NOT NULL,
                training_end TEXT NOT NULL,
                training_groups INTEGER NOT NULL,
                training_rows INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ranker_models_created
                ON ranker_models(created_at DESC);
            CREATE TABLE IF NOT EXISTS ranker_predictions (
                snapshot_id TEXT NOT NULL REFERENCES scan_snapshots(id) ON DELETE CASCADE,
                model_id TEXT NOT NULL REFERENCES ranker_models(id),
                score REAL NOT NULL,
                rank INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(snapshot_id,model_id)
            );
            CREATE INDEX IF NOT EXISTS ranker_predictions_model_rank
                ON ranker_predictions(model_id,rank);
            """
        )
        # These migrations keep existing beta databases usable. New fields are
        # nullable because older snapshots cannot be reconstructed exactly.
        for definition in (
            "scan_run_id TEXT REFERENCES scan_runs(id)",
            "baseline_rank INTEGER",
            "range_position REAL",
            "stale_minutes REAL",
            "session_volume INTEGER",
            "average_volume INTEGER",
            "average_dollar_volume REAL",
            "catalyst_kind TEXT",
            "catalyst_form TEXT",
            "catalyst_sentiment TEXT",
            "catalyst_score REAL",
            "catalyst_filed_at TEXT",
            "momentum_previous_5m_pct REAL",
            "momentum_acceleration_pct REAL",
            "intraday_volatility_pct REAL",
            "vwap_position_pct REAL",
            "pullback_from_high_pct REAL",
            "close_location REAL",
            "recent_dollar_volume REAL",
            "scoring_version TEXT",
        ):
            _ensure_column(db, "scan_snapshots", definition)
        for definition in (
            "barrier_label TEXT",
            "barrier_hit_at TEXT",
            "barrier_ambiguous INTEGER",
            "upper_barrier_pct REAL",
            "lower_barrier_pct REAL",
            "horizon_minutes INTEGER",
            "max_favorable_pct REAL",
            "max_adverse_pct REAL",
            "price_60m REAL",
            "return_60m_pct REAL",
            "observed_60m_at TEXT",
        ):
            _ensure_column(db, "scan_outcomes", definition)
        for definition in (
            "probability_up REAL",
            "probability_down REAL",
            "probability_timeout REAL",
            "expected_return_pct REAL",
        ):
            _ensure_column(db, "ranker_predictions", definition)
        _ensure_column(db, "sec_filings", "parser_version TEXT NOT NULL DEFAULT 'legacy'")
        _ensure_column(db, "watches", "last_seen_at TEXT")
        _ensure_column(
            db,
            "source_documents",
            "content_encoding TEXT NOT NULL DEFAULT 'identity'",
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS scan_snapshots_run_rank "
            "ON scan_snapshots(scan_run_id,baseline_rank)"
        )
