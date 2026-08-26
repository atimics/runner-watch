from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from runner_web.ai_kol import (
    DEFAULT_FLASH_MODEL,
    FLASH,
    actor_snapshot,
    model_display_name,
)
from runner_web.database import DatabaseConnection, initialize_sqlite, open_database
from runner_web.pseudonyms import ensure_scoped_alias
from runner_web.source_catalog import DEFAULT_SOURCE_POLICIES

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/runner-watch.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
REQUIRE_DATABASE_URL = os.getenv("REQUIRE_DATABASE_URL", "0") == "1"
REQUIRE_DATABASE_TLS = os.getenv("REQUIRE_DATABASE_TLS", "0") == "1"
MIGRATION_LOCK_ID = 7_348_195_620_341_977_301


def database_identity() -> str:
    """Return a safe cache namespace without exposing database credentials."""

    if DATABASE_URL:
        digest = sha256(DATABASE_URL.encode()).hexdigest()[:12]
        return f"postgres:{digest}"
    return f"sqlite:{DATABASE_PATH}"


def database_tls_enabled(database_url: str) -> bool:
    if not database_url:
        return False
    values = parse_qs(urlparse(database_url).query).get("sslmode", [])
    return bool(values and values[-1].lower() in {"require", "verify-ca", "verify-full"})


def _columns(db: DatabaseConnection, table: str) -> set[str]:
    if db.backend == "postgres":
        rows = db.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name=?
            """,
            (table,),
        ).fetchall()
        return {str(row["column_name"]) for row in rows}
    return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_column(db: DatabaseConnection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _seed_source_registry(db: DatabaseConnection) -> None:
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
def connection() -> Iterator[DatabaseConnection]:
    if REQUIRE_DATABASE_URL and not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required in this deployment")
    if REQUIRE_DATABASE_TLS and not database_tls_enabled(DATABASE_URL):
        raise RuntimeError("DATABASE_URL must require TLS in this deployment")
    with open_database(DATABASE_URL, DATABASE_PATH) as db:
        yield db


def _migration_001_baseline(db: DatabaseConnection) -> None:
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


def _migration_002_topic_snapshots(db: DatabaseConnection) -> None:
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


def _migration_003_ai_kol(db: DatabaseConnection) -> None:
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


def _migration_004_rug_risk(db: DatabaseConnection) -> None:
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


def _migration_005_performance_indexes(db: DatabaseConnection) -> None:
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


def _migration_006_identity_research(db: DatabaseConnection) -> None:
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


def _migration_007_pulse_attention(db: DatabaseConnection) -> None:
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


def _sync_flash_actor(db: DatabaseConnection) -> None:
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


def _migration_008_flash_actor(db: DatabaseConnection) -> None:
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
    known_flash_models = {DEFAULT_FLASH_MODEL, FLASH.model, "z-ai/glm-5.3"}
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


def _migration_009_request_path_indexes(db: DatabaseConnection) -> None:
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


def _migration_010_radar_indexes(db: DatabaseConnection) -> None:
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


def _migration_011_ticker_feedback(db: DatabaseConnection) -> None:
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


def _migration_012_chart_structure(db: DatabaseConnection) -> None:
    """Persist point-in-time chart structure for model training."""

    for definition in (
        "opening_range_position REAL",
        "opening_range_breakout_pct REAL",
        "support_distance_pct REAL",
        "support_strength REAL",
        "resistance_distance_pct REAL",
        "resistance_strength REAL",
        "fib_retracement_pct REAL",
        "fib_level_distance_pct REAL",
        "structure_available INTEGER NOT NULL DEFAULT 0",
        "fibonacci_available INTEGER NOT NULL DEFAULT 0",
    ):
        _ensure_column(db, "scan_snapshots", definition)


def _migration_013_comment_pseudonyms(db: DatabaseConnection) -> None:
    """Add stable public aliases for databases that already applied feedback."""

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS comment_pseudonyms (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            pseudonym TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def _migration_014_thesis_cases(db: DatabaseConnection) -> None:
    """Store living user theses and every revision made to them."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS thesis_cases (
            id TEXT PRIMARY KEY,
            public_id TEXT UNIQUE NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            source_kind TEXT NOT NULL DEFAULT 'short_note'
                CHECK(source_kind IN ('short_note','community_comment')),
            source_comment_id TEXT REFERENCES ticker_comments(id) ON DELETE SET NULL,
            thesis TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            reference_price REAL,
            reference_at TEXT NOT NULL,
            invalidation TEXT NOT NULL,
            risks_json TEXT NOT NULL DEFAULT '[]',
            questions_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','closed','archived')),
            final_outcome TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            closed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS thesis_cases_user_status_time
            ON thesis_cases(user_id,status,updated_at DESC);
        CREATE INDEX IF NOT EXISTS thesis_cases_ticker_status_time
            ON thesis_cases(ticker,status,updated_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS thesis_cases_active_source_comment
            ON thesis_cases(user_id,source_comment_id)
            WHERE source_comment_id IS NOT NULL AND status='active';
        CREATE TABLE IF NOT EXISTS thesis_case_revisions (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL REFERENCES thesis_cases(id) ON DELETE CASCADE,
            revision_no INTEGER NOT NULL,
            source_comment_id TEXT REFERENCES ticker_comments(id) ON DELETE SET NULL,
            thesis TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            reference_price REAL,
            reference_at TEXT NOT NULL,
            invalidation TEXT NOT NULL,
            risks_json TEXT NOT NULL,
            questions_json TEXT NOT NULL,
            confidence REAL,
            status TEXT NOT NULL,
            final_outcome TEXT,
            change_note TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(case_id,revision_no)
        );
        CREATE INDEX IF NOT EXISTS thesis_case_revisions_case_time
            ON thesis_case_revisions(case_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS thesis_case_updates (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL REFERENCES thesis_cases(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            direction TEXT NOT NULL
                CHECK(direction IN ('strengthened','weakened','unchanged','unknown')),
            summary TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            confidence_before REAL,
            confidence_after REAL,
            citations_json TEXT NOT NULL DEFAULT '[]',
            evidence_fingerprint TEXT NOT NULL,
            deterministic_veto_json TEXT NOT NULL DEFAULT '{}',
            model_provider TEXT,
            model_name TEXT,
            model_version TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(case_id,evidence_fingerprint)
        );
        CREATE INDEX IF NOT EXISTS thesis_case_updates_case_time
            ON thesis_case_updates(case_id,created_at DESC);
        CREATE TABLE IF NOT EXISTS thesis_case_seen (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            case_id TEXT NOT NULL REFERENCES thesis_cases(id) ON DELETE CASCADE,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(user_id,case_id)
        );
        """
    )

def _migration_015_evidence_claims(db: DatabaseConnection) -> None:
    """Group repeated source items into one real-world evidence claim."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS evidence_claims (
            id TEXT PRIMARY KEY,
            ticker TEXT NOT NULL,
            claim_key TEXT NOT NULL,
            claim_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            direction TEXT NOT NULL
                CHECK(direction IN ('supports','risks','neutral')),
            primary_source_type TEXT NOT NULL,
            primary_source_url TEXT,
            occurred_at TEXT NOT NULL,
            first_collected_at TEXT NOT NULL,
            last_collected_at TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 1,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(ticker,claim_key)
        );
        CREATE INDEX IF NOT EXISTS evidence_claims_ticker_collected
            ON evidence_claims(ticker,first_collected_at DESC);
        CREATE INDEX IF NOT EXISTS evidence_claims_ticker_occurred
            ON evidence_claims(ticker,occurred_at DESC);
        CREATE TABLE IF NOT EXISTS evidence_claim_sources (
            claim_id TEXT NOT NULL REFERENCES evidence_claims(id) ON DELETE CASCADE,
            source_key TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_url TEXT,
            title TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY(claim_id,source_key)
        );
        CREATE INDEX IF NOT EXISTS evidence_claim_sources_url
            ON evidence_claim_sources(source_url);
        CREATE TABLE IF NOT EXISTS thesis_case_claims (
            case_id TEXT NOT NULL REFERENCES thesis_cases(id) ON DELETE CASCADE,
            claim_id TEXT NOT NULL REFERENCES evidence_claims(id) ON DELETE CASCADE,
            linked_at TEXT NOT NULL,
            PRIMARY KEY(case_id,claim_id)
        );
        CREATE INDEX IF NOT EXISTS thesis_case_claims_claim
            ON thesis_case_claims(claim_id,case_id);
        """
    )


def _migration_016_research_stages(db: DatabaseConnection) -> None:
    """Freeze each role and model call in the verified research pipeline."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_stage_runs (
            id TEXT PRIMARY KEY,
            commission_id TEXT NOT NULL
                REFERENCES research_commissions(id) ON DELETE CASCADE,
            stage TEXT NOT NULL,
            stage_order INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('complete','failed')),
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            actor_snapshot_json TEXT NOT NULL,
            output_json TEXT NOT NULL DEFAULT '{}',
            usage_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            UNIQUE(commission_id,stage,input_fingerprint)
        );
        CREATE INDEX IF NOT EXISTS research_stage_runs_commission_order
            ON research_stage_runs(commission_id,stage_order);
        CREATE INDEX IF NOT EXISTS research_stage_runs_model_time
            ON research_stage_runs(provider,model,completed_at DESC);
        """
    )


def _migration_017_case_outcomes(db: DatabaseConnection) -> None:
    """Measure each private case at the horizon inferred from its comment."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS thesis_case_outcomes (
            case_id TEXT PRIMARY KEY REFERENCES thesis_cases(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            base_price REAL NOT NULL,
            base_at TEXT NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            due_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','complete')),
            end_price REAL,
            observed_at TEXT,
            return_pct REAL,
            return_direction TEXT CHECK(return_direction IN ('up','down','flat')),
            max_favorable_pct REAL,
            max_adverse_pct REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS thesis_case_outcomes_status_due
            ON thesis_case_outcomes(status,due_at);
        CREATE INDEX IF NOT EXISTS thesis_case_outcomes_ticker_due
            ON thesis_case_outcomes(ticker,due_at);
        """
    )


def _migration_018_research_policy_outcomes(db: DatabaseConnection) -> None:
    """Link model-policy reports to the private case outcomes that judge them."""

    for definition in (
        "case_id TEXT REFERENCES thesis_cases(id)",
        "case_effect TEXT",
        "market_view TEXT",
        "model_confidence REAL",
        "policy_version TEXT",
    ):
        _ensure_column(db, "research_commissions", definition)
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS research_commissions_case_time
            ON research_commissions(case_id,completed_at DESC);
        CREATE INDEX IF NOT EXISTS research_commissions_policy_model
            ON research_commissions(policy_version,model,completed_at DESC);
        """
    )
def _migration_019_repair_thesis_case_sources(db: DatabaseConnection) -> None:
    """Repair thesis tables created by an earlier version of migration 14."""

    _ensure_column(
        db,
        "thesis_cases",
        "source_kind TEXT NOT NULL DEFAULT 'short_note' "
        "CHECK(source_kind IN ('short_note','community_comment'))",
    )
    _ensure_column(
        db,
        "thesis_cases",
        "source_comment_id TEXT REFERENCES ticker_comments(id) ON DELETE SET NULL",
    )
    _ensure_column(
        db,
        "thesis_case_revisions",
        "source_comment_id TEXT REFERENCES ticker_comments(id) ON DELETE SET NULL",
    )
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS thesis_cases_active_source_comment
        ON thesis_cases(user_id,source_comment_id)
        WHERE source_comment_id IS NOT NULL AND status='active'
        """
    )


def _migration_020_user_positions(db: DatabaseConnection) -> None:
    """Store private entry and exit marks separately from public comments."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_positions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            entry_price REAL NOT NULL CHECK(entry_price>0),
            entry_at TEXT NOT NULL,
            exit_price REAL CHECK(exit_price IS NULL OR exit_price>0),
            exit_at TEXT,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','closed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(
                (status='active' AND exit_price IS NULL AND exit_at IS NULL)
                OR (status='closed' AND exit_price IS NOT NULL AND exit_at IS NOT NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS user_positions_user_ticker_time
            ON user_positions(user_id,ticker,entry_at DESC);
        CREATE INDEX IF NOT EXISTS user_positions_user_status_time
            ON user_positions(user_id,status,updated_at DESC);
        CREATE INDEX IF NOT EXISTS ticker_comments_public_time
            ON ticker_comments(status,created_at DESC);
        """
    )

def _migration_021_short_data(db: DatabaseConnection) -> None:
    """Cache current short and borrow facts and freeze them on each scan."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS short_data_cache (
            ticker TEXT PRIMARY KEY,
            short_interest_pct_float REAL,
            short_interest_shares REAL,
            days_to_cover REAL,
            short_interest_settlement_date TEXT,
            borrow_fee_pct REAL,
            shares_available REAL,
            borrow_observed_at TEXT,
            source TEXT NOT NULL,
            source_url TEXT,
            collected_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS short_data_cache_collected
            ON short_data_cache(collected_at DESC);
        """
    )
    for definition in (
        "short_interest_pct_float REAL",
        "short_interest_shares REAL",
        "days_to_cover REAL",
        "short_interest_settlement_date TEXT",
        "borrow_fee_pct REAL",
        "shares_available REAL",
        "borrow_observed_at TEXT",
        "short_data_source TEXT",
        "short_data_url TEXT",
        "short_data_collected_at TEXT",
    ):
        _ensure_column(db, "scan_snapshots", definition)


def _migration_022_public_calls_and_flash(db: DatabaseConnection) -> None:
    """Add immutable public Calls and Flash request audit records."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS community_calls (
            id TEXT PRIMARY KEY,
            public_id TEXT UNIQUE NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL DEFAULT 'long' CHECK(side IN ('long')),
            entry_price REAL NOT NULL CHECK(entry_price>0),
            entry_at TEXT NOT NULL,
            exit_price REAL CHECK(exit_price IS NULL OR exit_price>0),
            exit_at TEXT,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','closed')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(
                (status='active' AND exit_price IS NULL AND exit_at IS NULL)
                OR (status='closed' AND exit_price IS NOT NULL AND exit_at IS NOT NULL)
            )
        );
        CREATE UNIQUE INDEX IF NOT EXISTS community_calls_one_active
            ON community_calls(user_id,ticker) WHERE status='active';
        CREATE INDEX IF NOT EXISTS community_calls_user_status_time
            ON community_calls(user_id,status,updated_at DESC);
        CREATE INDEX IF NOT EXISTS community_calls_ticker_status_time
            ON community_calls(ticker,status,updated_at DESC);
        """
    )
    for definition in (
        "trigger TEXT NOT NULL DEFAULT 'commission'",
        "evidence_snapshot_json TEXT NOT NULL DEFAULT '{}'",
        "evidence_as_of TEXT",
        "citations_json TEXT NOT NULL DEFAULT '[]'",
    ):
        _ensure_column(db, "research_commissions", definition)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS flash_report_requests (
            id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL REFERENCES research_commissions(id) ON DELETE CASCADE,
            user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            trigger TEXT NOT NULL CHECK(trigger IN ('commission','community_auto')),
            created_new INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS flash_report_requests_report_time
            ON flash_report_requests(report_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS flash_report_requests_user_time
            ON flash_report_requests(user_id,created_at DESC);
        """
    )
def _migration_023_private_flash_commissions(db: DatabaseConnection) -> None:
    """Keep commissioned Flash reports isolated to the requesting user."""

    db.execute("DROP INDEX IF EXISTS research_commissions_running_shared")
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS research_commissions_running_actor
        ON research_commissions(user_id,ticker,actor_id) WHERE status='running'
        """
    )


def _migration_024_stripe_billing(db: DatabaseConnection) -> None:
    """Mirror Stripe subscription state without storing payment details."""

    for definition in (
        "stripe_customer_id TEXT",
        "stripe_subscription_id TEXT",
        "stripe_subscription_status TEXT NOT NULL DEFAULT 'none'",
        "stripe_subscription_price_id TEXT",
        "stripe_current_period_end TEXT",
        "stripe_cancel_at_period_end INTEGER NOT NULL DEFAULT 0",
        "billing_updated_at TEXT",
    ):
        _ensure_column(db, "users", definition)
    db.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS users_stripe_customer
            ON users(stripe_customer_id) WHERE stripe_customer_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS users_stripe_subscription
            ON users(stripe_subscription_id) WHERE stripe_subscription_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS stripe_webhook_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            received_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS stripe_webhook_events_type_time
            ON stripe_webhook_events(event_type,received_at DESC);
        """
    )


def _migration_025_flash_wallet(db: DatabaseConnection) -> None:
    """Add claimable Flash credits and explicit report publishing."""

    for definition in (
        "visibility TEXT NOT NULL DEFAULT 'private'",
        "published_at TEXT",
    ):
        _ensure_column(db, "research_commissions", definition)
    for definition in (
        "source TEXT NOT NULL DEFAULT 'user'",
        "generation_model TEXT",
    ):
        _ensure_column(db, "ticker_comments", definition)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS flash_wallets (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            balance INTEGER NOT NULL DEFAULT 0 CHECK(balance>=0),
            last_claim_on TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS flash_transactions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            amount INTEGER NOT NULL CHECK(amount<>0),
            kind TEXT NOT NULL,
            reference_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id,kind,reference_id)
        );
        CREATE INDEX IF NOT EXISTS flash_transactions_user_time
            ON flash_transactions(user_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS research_commissions_public_time
            ON research_commissions(visibility,published_at DESC);
        """
    )


def _migration_026_gdpr_privacy(db: DatabaseConnection) -> None:
    """Remove passive profiles and replace global public names with thread aliases."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS public_aliases (
            scope TEXT NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(scope,user_id),
            UNIQUE(scope,alias)
        );
        CREATE INDEX IF NOT EXISTS public_aliases_user
            ON public_aliases(user_id,scope);
        """
    )
    comment_threads = db.execute(
        "SELECT DISTINCT user_id,ticker FROM ticker_comments"
    ).fetchall()
    for row in comment_threads:
        ensure_scoped_alias(db, str(row["user_id"]), f"comment:{row['ticker']}")
    for table in (
        "activity_events",
        "ticker_hearts",
        "radar_seen",
        "pulse_profile_state",
        "ticker_reactions",
        "comment_pseudonyms",
    ):
        db.execute(f"DELETE FROM {table}")


def _migration_027_caller_identities(db: DatabaseConnection) -> None:
    """Give accounts unlinkable public caller IDs with permanent name tombstones."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS caller_identities (
            id TEXT PRIMARY KEY,
            handle TEXT NOT NULL UNIQUE,
            user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            status TEXT NOT NULL CHECK(status IN ('active','tombstoned')),
            claim_cost_cents INTEGER,
            payment_reference TEXT UNIQUE,
            claimed_at TEXT,
            deleted_at TEXT,
            CHECK(
                (status='active' AND user_id IS NOT NULL AND claimed_at IS NOT NULL)
                OR
                (status='tombstoned' AND user_id IS NULL AND claimed_at IS NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS caller_identities_owner
            ON caller_identities(user_id,claimed_at) WHERE user_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS caller_identity_claims (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            caller_identity_id TEXT REFERENCES caller_identities(id),
            payment_reference TEXT UNIQUE,
            claim_cost_cents INTEGER NOT NULL,
            free_claim INTEGER NOT NULL DEFAULT 0 CHECK(free_claim IN (0,1)),
            claimed_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS caller_identity_one_free_claim
            ON caller_identity_claims(user_id) WHERE free_claim=1;
        CREATE INDEX IF NOT EXISTS caller_identity_claims_owner
            ON caller_identity_claims(user_id,claimed_at);
        """
    )
    _ensure_column(
        db,
        "community_calls",
        "caller_identity_id TEXT REFERENCES caller_identities(id)",
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS community_calls_caller_time "
        "ON community_calls(caller_identity_id,updated_at DESC)"
    )


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    apply: Callable[[DatabaseConnection], None]


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
    Migration(12, "chart_structure", _migration_012_chart_structure),
    Migration(13, "comment_pseudonyms", _migration_013_comment_pseudonyms),
    Migration(14, "thesis_cases", _migration_014_thesis_cases),
    Migration(15, "evidence_claims", _migration_015_evidence_claims),
    Migration(16, "research_stages", _migration_016_research_stages),
    Migration(17, "case_outcomes", _migration_017_case_outcomes),
    Migration(18, "research_policy_outcomes", _migration_018_research_policy_outcomes),
    Migration(19, "repair_thesis_case_sources", _migration_019_repair_thesis_case_sources),
    Migration(20, "user_positions", _migration_020_user_positions),
    Migration(21, "short_data", _migration_021_short_data),
    Migration(22, "public_calls_and_flash", _migration_022_public_calls_and_flash),
    Migration(23, "private_flash_commissions", _migration_023_private_flash_commissions),
    Migration(24, "stripe_billing", _migration_024_stripe_billing),
    Migration(25, "flash_wallet", _migration_025_flash_wallet),
    Migration(26, "gdpr_privacy", _migration_026_gdpr_privacy),
    Migration(27, "caller_identities", _migration_027_caller_identities),
)


def _acquire_migration_lock(db: DatabaseConnection) -> bool:
    """Serialize PostgreSQL migrations across web, worker, and release processes."""

    if db.backend == "postgres":
        db.execute(
            "SELECT pg_advisory_lock(?)",
            (MIGRATION_LOCK_ID,),
        ).fetchone()
        return True
    return False


def _release_migration_lock(db: DatabaseConnection) -> None:
    db.execute(
        "SELECT pg_advisory_unlock(?)",
        (MIGRATION_LOCK_ID,),
    ).fetchone()


def _apply_migrations(db: DatabaseConnection) -> None:
    locked = _acquire_migration_lock(db)
    try:
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
    except BaseException:
        if locked:
            db.rollback()
        raise
    finally:
        if locked:
            _release_migration_lock(db)


def init_db() -> None:
    """Apply migrations, then refresh environment-based assignments and source policy."""

    if not DATABASE_URL:
        initialize_sqlite(DATABASE_PATH)
    with connection() as db:
        _apply_migrations(db)
        _sync_flash_actor(db)
        _seed_source_registry(db)


def main() -> None:
    """Apply the current database schema as a deployment release command."""

    init_db()
