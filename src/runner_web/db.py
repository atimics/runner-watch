from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from runner_web.ai_kol import (
    DEFAULT_FLASH_MODEL,
    FLASH,
    actor_snapshot,
    model_display_name,
)
from runner_web.source_catalog import DEFAULT_SOURCE_POLICIES

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/runner-watch.db"))


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(db: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _seed_source_registry(db: sqlite3.Connection) -> None:
    timestamp = datetime.now(UTC).isoformat()
    db.executemany(
        """
        INSERT INTO source_registry(
            source,feed,title,owner,terms_url,credential_env,
            expected_cadence_seconds,stale_after_seconds,schedule,
            storage_policy,display_policy,attribution,review_status,enabled,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source,feed) DO UPDATE SET
            title=excluded.title,owner=excluded.owner,terms_url=excluded.terms_url,
            credential_env=excluded.credential_env,
            expected_cadence_seconds=excluded.expected_cadence_seconds,
            stale_after_seconds=excluded.stale_after_seconds,schedule=excluded.schedule,
            storage_policy=excluded.storage_policy,display_policy=excluded.display_policy,
            attribution=excluded.attribution,review_status=excluded.review_status,
            enabled=excluded.enabled,
            updated_at=excluded.updated_at
        """,
        [
            (
                policy.source,
                policy.feed,
                policy.title,
                policy.owner,
                policy.terms_url,
                policy.credential_env,
                policy.expected_cadence_seconds,
                policy.stale_after_seconds,
                policy.schedule,
                policy.storage_policy,
                policy.display_policy,
                policy.attribution,
                policy.review_status,
                int(policy.enabled),
                timestamp,
                timestamp,
            )
            for policy in DEFAULT_SOURCE_POLICIES
        ],
    )


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _migration_001_baseline(db: sqlite3.Connection) -> None:
    with db:
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
            CREATE TABLE IF NOT EXISTS activity_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                event_type TEXT NOT NULL,
                weight REAL NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS activity_events_profile_time
                ON activity_events(profile_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS activity_events_ticker_time
                ON activity_events(ticker,created_at DESC);
            CREATE TABLE IF NOT EXISTS ticker_hearts (
                profile_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(profile_id,ticker)
            );
            CREATE INDEX IF NOT EXISTS ticker_hearts_rank
                ON ticker_hearts(active,ticker,updated_at DESC);
            CREATE TABLE IF NOT EXISTS radar_seen (
                profile_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(profile_id,ticker)
            );
            CREATE TABLE IF NOT EXISTS alpha_reports (
                id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                evidence_key TEXT NOT NULL,
                status TEXT NOT NULL,
                model TEXT,
                headline TEXT,
                summary TEXT,
                catalysts_json TEXT NOT NULL DEFAULT '[]',
                risks_json TEXT NOT NULL DEFAULT '[]',
                watch_json TEXT NOT NULL DEFAULT '[]',
                sources_json TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(ticker,evidence_key)
            );
            CREATE INDEX IF NOT EXISTS alpha_reports_ticker_time
                ON alpha_reports(ticker,created_at DESC);
            CREATE TABLE IF NOT EXISTS research_commissions (
                id TEXT PRIMARY KEY,
                public_id TEXT UNIQUE NOT NULL,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                ticker TEXT NOT NULL,
                evidence_key TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_model TEXT NOT NULL,
                model TEXT,
                headline TEXT,
                summary TEXT,
                catalysts_json TEXT NOT NULL DEFAULT '[]',
                risks_json TEXT NOT NULL DEFAULT '[]',
                watch_json TEXT NOT NULL DEFAULT '[]',
                sources_json TEXT NOT NULL DEFAULT '[]',
                usage_json TEXT NOT NULL DEFAULT '{}',
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS research_commissions_created
                ON research_commissions(created_at DESC);
            CREATE INDEX IF NOT EXISTS research_commissions_ticker
                ON research_commissions(ticker,completed_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS research_commissions_running
                ON research_commissions(user_id,ticker) WHERE status='running';
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
            CREATE TABLE IF NOT EXISTS source_registry (
                source TEXT NOT NULL,
                feed TEXT NOT NULL,
                title TEXT NOT NULL,
                owner TEXT NOT NULL,
                terms_url TEXT,
                credential_env TEXT,
                expected_cadence_seconds INTEGER,
                stale_after_seconds INTEGER,
                schedule TEXT NOT NULL DEFAULT 'event',
                storage_policy TEXT NOT NULL,
                display_policy TEXT NOT NULL,
                attribution TEXT,
                review_status TEXT NOT NULL DEFAULT 'review_required',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(source,feed)
            );
            CREATE TABLE IF NOT EXISTS security_quotes (
                source TEXT NOT NULL,
                feed TEXT NOT NULL,
                ticker TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                bid REAL,
                ask REAL,
                bid_size REAL,
                ask_size REAL,
                last_trade REAL,
                exchange TEXT,
                conditions_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                first_run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
                last_run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
                first_collected_at TEXT NOT NULL,
                last_collected_at TEXT NOT NULL,
                PRIMARY KEY(source,feed,ticker,observed_at)
            );
            CREATE INDEX IF NOT EXISTS security_quotes_ticker_time
                ON security_quotes(ticker,observed_at DESC);
            CREATE TABLE IF NOT EXISTS market_events (
                source TEXT NOT NULL,
                feed TEXT NOT NULL,
                event_id TEXT NOT NULL,
                version TEXT NOT NULL,
                ticker TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_at TEXT NOT NULL,
                published_at TEXT,
                effective_at TEXT,
                status TEXT NOT NULL,
                source_url TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                first_run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
                last_run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
                first_collected_at TEXT NOT NULL,
                last_collected_at TEXT NOT NULL,
                PRIMARY KEY(source,feed,event_id,version)
            );
            CREATE INDEX IF NOT EXISTS market_events_ticker_time
                ON market_events(ticker,event_at DESC);
            CREATE INDEX IF NOT EXISTS market_events_type_time
                ON market_events(event_type,event_at DESC);
            CREATE TABLE IF NOT EXISTS issuer_facts (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                feed TEXT NOT NULL,
                cik INTEGER NOT NULL,
                concept TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                period_start TEXT,
                period_end TEXT NOT NULL,
                filed_at TEXT NOT NULL,
                accession TEXT NOT NULL,
                form TEXT,
                source_tag TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                first_run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
                last_run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
                first_collected_at TEXT NOT NULL,
                last_collected_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS issuer_facts_cik_concept_filed
                ON issuer_facts(cik,concept,filed_at DESC);
            CREATE TABLE IF NOT EXISTS entity_links (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                feed TEXT NOT NULL,
                external_id TEXT NOT NULL,
                cik INTEGER,
                ticker TEXT NOT NULL,
                confidence REAL NOT NULL,
                method TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                first_run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
                last_run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
                first_collected_at TEXT NOT NULL,
                last_collected_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS entity_links_ticker
                ON entity_links(ticker,confidence DESC);
            CREATE INDEX IF NOT EXISTS entity_links_external
                ON entity_links(source,external_id);
            CREATE TABLE IF NOT EXISTS macro_observations (
                source TEXT NOT NULL,
                feed TEXT NOT NULL,
                series_id TEXT NOT NULL,
                observation_date TEXT NOT NULL,
                vintage_date TEXT NOT NULL,
                value REAL NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                first_run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
                last_run_id TEXT NOT NULL REFERENCES ingestion_runs(id),
                first_collected_at TEXT NOT NULL,
                last_collected_at TEXT NOT NULL,
                PRIMARY KEY(source,series_id,observation_date,vintage_date)
            );
            CREATE INDEX IF NOT EXISTS macro_observations_series_date
                ON macro_observations(series_id,observation_date DESC,vintage_date DESC);
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
        _ensure_column(db, "users", "plan TEXT NOT NULL DEFAULT 'free'")
        _ensure_column(
            db,
            "source_documents",
            "content_encoding TEXT NOT NULL DEFAULT 'identity'",
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS scan_snapshots_run_rank "
            "ON scan_snapshots(scan_run_id,baseline_rank)"
        )


def _migration_002_topic_snapshots(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS topic_snapshots (
            topic TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            as_of TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            delayed INTEGER,
            payload_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            error TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS topic_snapshots_collected
            ON topic_snapshots(collected_at DESC);
        """
    )


def _migration_003_ai_kol(db: sqlite3.Connection) -> None:
    """Add immutable AI KOL calls without mixing them with human hearts."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS kol_predictors (
            id TEXT PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            emoji TEXT NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'shadow',
            visible INTEGER NOT NULL DEFAULT 0,
            contract_version TEXT NOT NULL,
            upper_barrier_pct REAL NOT NULL,
            lower_barrier_pct REAL NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            min_probability_up REAL NOT NULL,
            min_expected_return_pct REAL NOT NULL,
            abandon_probability_up REAL NOT NULL,
            abandon_expected_return_pct REAL NOT NULL,
            max_calls_per_scan INTEGER NOT NULL,
            max_active_calls INTEGER NOT NULL,
            paper_notional REAL NOT NULL,
            round_trip_cost_bps REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kol_calls (
            id TEXT PRIMARY KEY,
            predictor_id TEXT NOT NULL REFERENCES kol_predictors(id),
            model_id TEXT NOT NULL REFERENCES ranker_models(id),
            snapshot_id TEXT NOT NULL REFERENCES scan_snapshots(id) ON DELETE CASCADE,
            scan_run_id TEXT NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            upper_barrier_pct REAL NOT NULL,
            lower_barrier_pct REAL NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            status TEXT NOT NULL,
            confidence REAL NOT NULL,
            expected_return_pct REAL NOT NULL,
            entry_price REAL NOT NULL,
            entry_at TEXT NOT NULL,
            last_price REAL NOT NULL,
            last_mark_at TEXT NOT NULL,
            unrealized_return_pct REAL NOT NULL DEFAULT 0,
            exit_price REAL,
            exit_at TEXT,
            realized_return_pct REAL,
            net_return_pct REAL,
            paper_notional REAL NOT NULL,
            round_trip_cost_bps REAL NOT NULL,
            paper_pnl REAL,
            close_reason TEXT,
            benchmark_label TEXT,
            benchmark_return_60m_pct REAL,
            benchmark_at TEXT,
            max_favorable_pct REAL NOT NULL DEFAULT 0,
            max_adverse_pct REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(predictor_id,snapshot_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS kol_calls_one_active_ticker
            ON kol_calls(predictor_id,ticker) WHERE status='active';
        CREATE INDEX IF NOT EXISTS kol_calls_ticker_time
            ON kol_calls(ticker,created_at DESC);
        CREATE INDEX IF NOT EXISTS kol_calls_predictor_time
            ON kol_calls(predictor_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS kol_calls_status_time
            ON kol_calls(status,updated_at DESC);
        CREATE TABLE IF NOT EXISTS kol_call_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT NOT NULL REFERENCES kol_calls(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            price REAL,
            return_pct REAL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS kol_call_events_call_time
            ON kol_call_events(call_id,event_at);
        """
    )


def _migration_004_rug_risk(db: sqlite3.Connection) -> None:
    """Store separate setup, rug, crash, and state evidence."""

    timestamp = datetime.now(UTC).isoformat()
    for definition in (
        "setup_score REAL",
        "rug_score REAL",
        "rug_level TEXT",
        "trade_state TEXT",
        "state_reason TEXT",
        "hard_veto INTEGER NOT NULL DEFAULT 0",
        "crash_candidate INTEGER NOT NULL DEFAULT 0",
        "drawdown_20d_pct REAL",
        "drawdown_90d_pct REAL",
        "drawdown_52w_pct REAL",
        "rebound_from_20d_low_pct REAL",
        "risk_factors_json TEXT NOT NULL DEFAULT '[]'",
        "issuer_risk_json TEXT NOT NULL DEFAULT '{}'",
    ):
        _ensure_column(db, "scan_snapshots", definition)
    for definition in (
        "post_transaction_shares REAL",
        "stake_change_pct REAL",
        "is_10b5_1 INTEGER NOT NULL DEFAULT 0",
        "direct_ownership INTEGER",
        "footnotes TEXT NOT NULL DEFAULT ''",
        "beneficial_ownership_pct REAL",
        "beneficial_shares REAL",
        "reporting_person_types TEXT NOT NULL DEFAULT ''",
    ):
        _ensure_column(db, "sec_filings", definition)
    db.execute(
        "CREATE INDEX IF NOT EXISTS scan_snapshots_trade_state "
        "ON scan_snapshots(trade_state,rug_score,captured_at DESC)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS scan_snapshots_crash "
        "ON scan_snapshots(crash_candidate,drawdown_52w_pct,captured_at DESC)"
    )
    db.execute(
        """
        INSERT OR IGNORE INTO kol_predictors(
            id,slug,display_name,emoji,description,status,visible,contract_version,
            upper_barrier_pct,lower_barrier_pct,horizon_minutes,min_probability_up,
            min_expected_return_pct,abandon_probability_up,
            abandon_expected_return_pct,max_calls_per_scan,max_active_calls,
            paper_notional,round_trip_cost_bps,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "kol-flash",
            "flash",
            "Flash",
            "⚡",
            "The public runner champion backed by the latest calibrated ranker.",
            "champion",
            1,
            "runner-8-4-60-v1",
            8.0,
            4.0,
            60,
            0.55,
            0.0,
            0.25,
            -1.0,
            1,
            3,
            1000.0,
            50.0,
            timestamp,
            timestamp,
        ),
    )


def _migration_005_performance_indexes(db: sqlite3.Connection) -> None:
    """Keep risk lookups bounded as collected history grows."""

    db.execute(
        "CREATE INDEX IF NOT EXISTS scan_snapshots_ticker_state_captured "
        "ON scan_snapshots(ticker,captured_at DESC,trade_state) "
        "WHERE trade_state IS NOT NULL"
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS market_events_risk_ticker_time
        ON market_events(ticker,event_at DESC)
        WHERE event_type IN (
            'trading_halt','reverse_split','corporate_action','security_action'
        )
        """
    )


def _migration_006_identity_research(db: sqlite3.Connection) -> None:
    """Store thesis-first company, person, and filing research."""

    _ensure_column(
        db,
        "sec_filings",
        "beneficial_owner_names TEXT NOT NULL DEFAULT ''",
    )
    for definition in (
        "thesis TEXT",
        "company_profile_json TEXT NOT NULL DEFAULT '{}'",
        "people_json TEXT NOT NULL DEFAULT '[]'",
        "filing_context_json TEXT NOT NULL DEFAULT '[]'",
        "unknowns_json TEXT NOT NULL DEFAULT '[]'",
        "research_mode TEXT NOT NULL DEFAULT 'evidence_only'",
    ):
        _ensure_column(db, "research_commissions", definition)


def _migration_007_pulse_attention(db: sqlite3.Connection) -> None:
    """Track each profile's attention state for a real Pulse entry."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS pulse_profile_state (
            profile_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            entered_at TEXT NOT NULL,
            first_seen_at TEXT,
            last_seen_at TEXT,
            inspected_at TEXT,
            notified_at TEXT,
            PRIMARY KEY(profile_id,ticker,entered_at)
        );
        CREATE INDEX IF NOT EXISTS pulse_profile_state_profile_entry
            ON pulse_profile_state(profile_id,entered_at DESC);
        CREATE INDEX IF NOT EXISTS pulse_profile_state_ticker_entry
            ON pulse_profile_state(ticker,entered_at DESC);
        """
    )


def _sync_flash_actor(db: sqlite3.Connection) -> None:
    """Keep Flash's live model assignment in sync without changing its identity."""

    row = db.execute("SELECT * FROM kol_predictors WHERE id=?", (FLASH.id,)).fetchone()
    if not row:
        raise RuntimeError("The Flash KOL seed is missing")
    current = dict(row)
    expected = {
        "slot": FLASH.slot,
        "ladder_position": FLASH.ladder_position,
        "inference_provider": FLASH.provider,
        "inference_model": FLASH.model,
        "display_name": FLASH.display_name,
        "emoji": FLASH.emoji,
        "description": FLASH.description,
    }
    if all(current.get(key) == value for key, value in expected.items()):
        return
    timestamp = datetime.now(UTC).isoformat()
    assigned_at = (
        current.get("model_assigned_at")
        if current.get("inference_model") == FLASH.model
        else timestamp
    ) or timestamp
    db.execute(
        """
        UPDATE kol_predictors SET
            slot=?,ladder_position=?,inference_provider=?,inference_model=?,
            model_assigned_at=?,display_name=?,emoji=?,description=?,updated_at=?
        WHERE id=?
        """,
        (
            FLASH.slot,
            FLASH.ladder_position,
            FLASH.provider,
            FLASH.model,
            assigned_at,
            FLASH.display_name,
            FLASH.emoji,
            FLASH.description,
            timestamp,
            FLASH.id,
        ),
    )


def _migration_008_flash_actor(db: sqlite3.Connection) -> None:
    """Make Flash a durable KOL slot with a replaceable model assignment."""

    for definition in (
        "slot TEXT",
        "ladder_position INTEGER",
        "inference_provider TEXT",
        "inference_model TEXT",
        "model_assigned_at TEXT",
    ):
        _ensure_column(db, "kol_predictors", definition)
    _sync_flash_actor(db)
    for definition in (
        "actor_id TEXT",
        "actor_snapshot_json TEXT NOT NULL DEFAULT '{}'",
    ):
        _ensure_column(db, "research_commissions", definition)
    known_flash_models = {DEFAULT_FLASH_MODEL, FLASH.model}
    historical = db.execute(
        """
        SELECT id,requested_model,model FROM research_commissions
        WHERE actor_id IS NULL
        """
    ).fetchall()
    for row in historical:
        requested_model = str(row["requested_model"])
        returned_model = str(row["model"] or "")
        if requested_model not in known_flash_models and returned_model not in known_flash_models:
            continue
        snapshot = actor_snapshot(FLASH)
        snapshot["model"] = requested_model
        snapshot["model_label"] = model_display_name(requested_model)
        db.execute(
            """
            UPDATE research_commissions SET actor_id=?,actor_snapshot_json=? WHERE id=?
            """,
            (
                FLASH.id,
                json.dumps(snapshot, separators=(",", ":")),
                row["id"],
            ),
        )
    _ensure_column(
        db,
        "kol_calls",
        "actor_snapshot_json TEXT NOT NULL DEFAULT '{}'",
    )
    legacy_call_snapshot = actor_snapshot(FLASH)
    legacy_call_snapshot["attribution"] = "backfilled_at_flash_slot_launch"
    legacy_call_snapshot["authorship"] = "deterministic_signal_policy"
    db.execute(
        """
        UPDATE kol_calls SET actor_snapshot_json=?
        WHERE predictor_id=? AND actor_snapshot_json='{}'
        """,
        (
            json.dumps(legacy_call_snapshot, separators=(",", ":")),
            FLASH.id,
        ),
    )
    db.execute("DROP INDEX IF EXISTS research_commissions_running")
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS research_commissions_running_actor
        ON research_commissions(user_id,ticker,actor_id) WHERE status='running'
        """
    )


def _migration_009_request_path_indexes(db: sqlite3.Connection) -> None:
    """Avoid table scans on the live Pulse request path."""

    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS market_events_event_time
            ON market_events(event_at DESC,last_collected_at DESC);
        CREATE INDEX IF NOT EXISTS sec_filings_created
            ON sec_filings(created_at DESC);
        CREATE INDEX IF NOT EXISTS scan_runs_candidate_captured
            ON scan_runs(captured_at DESC) WHERE candidate_rows>0;
        """
    )


def _migration_010_radar_indexes(db: sqlite3.Connection) -> None:
    """Support Radar's latest-event and latest-snapshot batch reads."""

    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS scan_snapshots_ticker_captured
            ON scan_snapshots(ticker,captured_at DESC);
        CREATE INDEX IF NOT EXISTS market_events_ticker_event_time
            ON market_events(ticker,event_at DESC,last_collected_at DESC);
        CREATE INDEX IF NOT EXISTS sec_filings_ticker_filed_score
            ON sec_filings(ticker,filed_at DESC,score DESC);
        """
    )


def _migration_011_ticker_feedback(db: sqlite3.Connection) -> None:
    """Store one bull/bear reaction per profile and signed-in comments."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS ticker_reactions (
            profile_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            reaction TEXT NOT NULL CHECK(reaction IN ('bull','bear')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(profile_id,ticker)
        );
        CREATE INDEX IF NOT EXISTS ticker_reactions_ticker_reaction
            ON ticker_reactions(ticker,reaction,updated_at DESC);
        CREATE TABLE IF NOT EXISTS comment_pseudonyms (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            pseudonym TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ticker_comments (
            id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'public' CHECK(status IN ('public','hidden')),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ticker_comments_ticker_time
            ON ticker_comments(ticker,status,created_at DESC);
        """
    )


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


MIGRATIONS = (
    Migration(1, "baseline", _migration_001_baseline),
    Migration(2, "topic_snapshots", _migration_002_topic_snapshots),
    Migration(3, "ai_kol", _migration_003_ai_kol),
    Migration(4, "rug_risk", _migration_004_rug_risk),
    Migration(5, "performance_indexes", _migration_005_performance_indexes),
    Migration(6, "identity_research", _migration_006_identity_research),
    Migration(7, "pulse_attention", _migration_007_pulse_attention),
    Migration(8, "flash_actor", _migration_008_flash_actor),
    Migration(9, "request_path_indexes", _migration_009_request_path_indexes),
    Migration(10, "radar_indexes", _migration_010_radar_indexes),
    Migration(11, "ticker_feedback", _migration_011_ticker_feedback),
)


def _apply_migrations(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row["version"]): str(row["name"])
        for row in db.execute("SELECT version,name FROM schema_migrations").fetchall()
    }
    known_versions = {migration.version for migration in MIGRATIONS}
    unknown = sorted(set(applied) - known_versions)
    if unknown:
        raise RuntimeError(f"Database has migrations newer than this app: {unknown}")
    for migration in MIGRATIONS:
        applied_name = applied.get(migration.version)
        if applied_name is not None:
            if applied_name != migration.name:
                raise RuntimeError(
                    f"Migration {migration.version} is recorded as {applied_name!r}, "
                    f"expected {migration.name!r}"
                )
            continue
        migration.apply(db)
        db.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (migration.version, migration.name, datetime.now(UTC).isoformat()),
        )


def init_db() -> None:
    """Apply migrations, then refresh environment-based assignments and source policy."""

    with connection() as db:
        _apply_migrations(db)
        _sync_flash_actor(db)
        _seed_source_registry(db)
