from __future__ import annotations

import json
import os
import secrets
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from runner_watch.source_catalog import DEFAULT_SOURCE_POLICIES
from runner_web.ai_kol import (
    DEFAULT_FLASH_MODEL,
    FLASH,
    actor_snapshot,
    flash_version_snapshot,
    model_display_name,
)
from runner_web.database import (
    DatabaseConnection,
    close_database_pool,
    initialize_sqlite,
    open_database,
)
from runner_web.pseudonyms import (
    ADJECTIVES,
    ANIMALS,
    ensure_comment_avatar,
    ensure_scoped_alias,
    migrate_comment_aliases_to_glyphs,
)

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "data/runner-watch.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
REQUIRE_DATABASE_URL = os.getenv("REQUIRE_DATABASE_URL", "0") == "1"
REQUIRE_DATABASE_TLS = os.getenv("REQUIRE_DATABASE_TLS", "0") == "1"
ALLOW_NEWER_DATABASE_SCHEMA = os.getenv("ALLOW_NEWER_DATABASE_SCHEMA", "0") == "1"
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


def database_url_with_required_tls(database_url: str) -> str:
    """Add TLS when it is required, while rejecting an explicit unsafe mode."""

    if not database_url:
        raise RuntimeError("DATABASE_URL must require TLS in this deployment")
    if database_tls_enabled(database_url):
        return database_url

    parsed = urlparse(database_url)
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.lower() == "sslmode" for key, _value in parameters):
        raise RuntimeError("DATABASE_URL must require TLS in this deployment")

    parameters.append(("sslmode", "require"))
    return urlunparse(parsed._replace(query=urlencode(parameters)))


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
    database_url = (
        database_url_with_required_tls(DATABASE_URL) if REQUIRE_DATABASE_TLS else DATABASE_URL
    )
    with open_database(database_url, DATABASE_PATH) as db:
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


def _migration_026_daily_report_alpha(db: DatabaseConnection) -> None:
    """Make each ticker's daily Flash report exclusive for its first hour."""

    for definition in (
        "report_day TEXT",
        "exclusive_until TEXT",
    ):
        _ensure_column(db, "research_commissions", definition)
    db.executescript(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS research_commissions_daily_actor
            ON research_commissions(ticker,actor_id,report_day)
            WHERE report_day IS NOT NULL AND status IN ('running','complete');
        CREATE INDEX IF NOT EXISTS research_commissions_daily_visibility
            ON research_commissions(report_day,visibility,exclusive_until);
        """
    )


def _migration_031_scalable_scan_storage(db: DatabaseConnection) -> None:
    """Store small read and training records separately from full scan snapshots."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS pulse_entries (
            ticker TEXT NOT NULL,
            entered_at TEXT NOT NULL,
            scan_run_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            price REAL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(ticker,entered_at)
        );
        CREATE INDEX IF NOT EXISTS pulse_entries_ticker_time
            ON pulse_entries(ticker,entered_at DESC);
        CREATE INDEX IF NOT EXISTS pulse_entries_time
            ON pulse_entries(entered_at DESC);

        CREATE TABLE IF NOT EXISTS ranker_training_examples (
            snapshot_id TEXT PRIMARY KEY,
            scan_run_id TEXT NOT NULL,
            ticker TEXT NOT NULL,
            feature_schema_version TEXT NOT NULL,
            expected_candidates INTEGER NOT NULL,
            captured_at TEXT NOT NULL,
            feature_vector_json TEXT NOT NULL,
            baseline_score_milli INTEGER NOT NULL,
            barrier_label TEXT,
            outcome_return_bp INTEGER,
            labeled_at TEXT
        );
        CREATE INDEX IF NOT EXISTS ranker_training_examples_schema_time
            ON ranker_training_examples(feature_schema_version,captured_at DESC,scan_run_id);
        CREATE INDEX IF NOT EXISTS ranker_training_examples_labeled_time
            ON ranker_training_examples(feature_schema_version,captured_at DESC)
            WHERE barrier_label IS NOT NULL;

        CREATE INDEX IF NOT EXISTS market_bars_collected
            ON market_bars(last_collected_at);
        """
    )

    cutoff = (datetime.now(UTC) - timedelta(days=6)).isoformat()
    db.execute(
        """
        WITH recent_runs AS (
            SELECT id,captured_at FROM scan_runs
            WHERE candidate_rows>0 AND captured_at>=?
        ),
        prior_run AS (
            SELECT id,captured_at FROM scan_runs
            WHERE candidate_rows>0 AND captured_at<?
            ORDER BY captured_at DESC,id DESC LIMIT 1
        ),
        ordered_source AS (
            SELECT id,captured_at FROM recent_runs
            UNION ALL
            SELECT id,captured_at FROM prior_run
        ),
        ordered_runs AS (
            SELECT id,captured_at,
                   LAG(id) OVER (ORDER BY captured_at,id) AS previous_run_id
            FROM ordered_source
        )
        INSERT INTO pulse_entries(
            ticker,entered_at,scan_run_id,snapshot_id,price,created_at
        )
        SELECT current.ticker,current.captured_at,current.scan_run_id,
               current.id,current.price,current.captured_at
        FROM ordered_runs run
        JOIN scan_snapshots current ON current.scan_run_id=run.id
        LEFT JOIN scan_snapshots previous
          ON previous.scan_run_id=run.previous_run_id
         AND previous.ticker=current.ticker
        WHERE run.captured_at>=? AND previous.id IS NULL
        ON CONFLICT(ticker,entered_at) DO NOTHING
        """,
        (cutoff, cutoff, cutoff),
    )


def _migration_027_gdpr_privacy(db: DatabaseConnection) -> None:
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
    comment_threads = db.execute("SELECT DISTINCT user_id,ticker FROM ticker_comments").fetchall()
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


def _migration_028_caller_identities(db: DatabaseConnection) -> None:
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
    migrated_at = datetime.now(UTC).isoformat()
    owners = db.execute(
        "SELECT DISTINCT user_id FROM community_calls WHERE caller_identity_id IS NULL"
    ).fetchall()
    for owner in owners:
        caller_identity_id = _migrated_caller_identity(db, str(owner["user_id"]), migrated_at)
        db.execute(
            "UPDATE community_calls SET caller_identity_id=? "
            "WHERE user_id=? AND caller_identity_id IS NULL",
            (caller_identity_id, owner["user_id"]),
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS community_calls_caller_time "
        "ON community_calls(caller_identity_id,updated_at DESC)"
    )


def _migration_029_signal_caller_identities(db: DatabaseConnection) -> None:
    """Stop exposing account names on human calls and require a separate caller ID."""

    _ensure_column(
        db,
        "signals",
        "caller_identity_id TEXT REFERENCES caller_identities(id)",
    )
    migrated_at = datetime.now(UTC).isoformat()
    owners = db.execute(
        "SELECT DISTINCT user_id FROM signals "
        "WHERE user_id IS NOT NULL AND caller_identity_id IS NULL"
    ).fetchall()
    for owner in owners:
        caller_identity_id = _migrated_caller_identity(db, str(owner["user_id"]), migrated_at)
        db.execute(
            "UPDATE signals SET caller_identity_id=? "
            "WHERE user_id=? AND caller_identity_id IS NULL",
            (caller_identity_id, owner["user_id"]),
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS signals_caller_identity "
        "ON signals(caller_identity_id,created_at DESC)"
    )


def _migrated_caller_identity(
    db: DatabaseConnection,
    user_id: str,
    claimed_at: str,
) -> str:
    """Give existing Calls one random animal ID without reviving account names."""

    existing = db.execute(
        "SELECT id FROM caller_identities "
        "WHERE user_id=? AND status='active' ORDER BY claimed_at,id LIMIT 1",
        (user_id,),
    ).fetchone()
    if existing:
        return str(existing["id"])
    handles = [f"{adjective}-{animal}" for adjective in ADJECTIVES for animal in ANIMALS]
    secrets.SystemRandom().shuffle(handles)
    for handle in handles:
        identity_id = str(uuid.uuid4())
        inserted = db.execute(
            """
            INSERT INTO caller_identities(
                id,handle,user_id,status,claim_cost_cents,claimed_at
            ) VALUES(?,?,?,'active',0,?) ON CONFLICT DO NOTHING
            """,
            (identity_id, handle, user_id, claimed_at),
        )
        if inserted.rowcount == 0:
            continue
        db.execute(
            """
            INSERT INTO caller_identity_claims(
                id,user_id,caller_identity_id,claim_cost_cents,free_claim,claimed_at
            ) VALUES(?,?,?,0,1,?)
            """,
            (str(uuid.uuid4()), user_id, identity_id, claimed_at),
        )
        return identity_id
    raise RuntimeError("The caller-ID name space is full")


def _migration_030_drop_passive_tracking(db: DatabaseConnection) -> None:
    """Permanently remove the legacy behavioural tracking schema."""

    db.executescript(
        """
        DROP TABLE IF EXISTS activity_events;
        DROP TABLE IF EXISTS ticker_hearts;
        DROP TABLE IF EXISTS radar_seen;
        DROP TABLE IF EXISTS pulse_profile_state;
        DROP TABLE IF EXISTS ticker_reactions;
        DROP TABLE IF EXISTS comment_pseudonyms;
        """
    )


def _migration_032_sports_domain(db: DatabaseConnection) -> None:
    """Add source-bound sports events, odds, predictions, and paper picks."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sports_events (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            external_id TEXT NOT NULL,
            league TEXT NOT NULL,
            season_type TEXT NOT NULL DEFAULT 'unknown',
            name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pre','in','post')),
            status_detail TEXT NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0,
            home_team_id TEXT NOT NULL,
            home_team_name TEXT NOT NULL,
            home_abbreviation TEXT NOT NULL,
            home_record TEXT,
            home_score REAL,
            away_team_id TEXT NOT NULL,
            away_team_name TEXT NOT NULL,
            away_abbreviation TEXT NOT NULL,
            away_record TEXT,
            away_score REAL,
            venue TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL,
            first_collected_at TEXT NOT NULL,
            last_collected_at TEXT NOT NULL,
            UNIQUE(provider,league,external_id)
        );
        CREATE INDEX IF NOT EXISTS sports_events_league_start
            ON sports_events(league,start_time,status);

        CREATE TABLE IF NOT EXISTS sports_odds_snapshots (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES sports_events(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            sportsbook TEXT NOT NULL,
            market TEXT NOT NULL CHECK(market IN ('moneyline')),
            home_odds INTEGER,
            away_odds INTEGER,
            home_open_odds INTEGER,
            away_open_odds INTEGER,
            spread REAL,
            total REAL,
            snapshot_hash TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(event_id,provider,market,snapshot_hash)
        );
        CREATE INDEX IF NOT EXISTS sports_odds_event_time
            ON sports_odds_snapshots(event_id,observed_at DESC);

        CREATE TABLE IF NOT EXISTS sports_predictions (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES sports_events(id) ON DELETE CASCADE,
            model_version TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            selection TEXT NOT NULL CHECK(selection IN ('home','away','pass')),
            home_probability REAL NOT NULL,
            away_probability REAL NOT NULL,
            home_market_probability REAL,
            away_market_probability REAL,
            edge REAL,
            signal TEXT NOT NULL,
            quality TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '[]',
            risks_json TEXT NOT NULL DEFAULT '[]',
            observed_at TEXT NOT NULL,
            UNIQUE(event_id,model_version,input_hash)
        );
        CREATE INDEX IF NOT EXISTS sports_predictions_event_time
            ON sports_predictions(event_id,observed_at DESC);

        CREATE TABLE IF NOT EXISTS sports_picks (
            id TEXT PRIMARY KEY,
            public_id TEXT UNIQUE NOT NULL,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            caller_identity_id TEXT NOT NULL REFERENCES caller_identities(id),
            event_id TEXT NOT NULL REFERENCES sports_events(id),
            market TEXT NOT NULL CHECK(market IN ('moneyline')),
            selection TEXT NOT NULL CHECK(selection IN ('home','away')),
            line REAL,
            american_odds INTEGER NOT NULL CHECK(american_odds<>0),
            sportsbook TEXT NOT NULL,
            odds_observed_at TEXT NOT NULL,
            prediction_id TEXT REFERENCES sports_predictions(id) ON DELETE SET NULL,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','settled','void')),
            result TEXT CHECK(result IS NULL OR result IN ('win','loss','push','void')),
            return_units REAL,
            settled_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id,event_id,market)
        );
        CREATE INDEX IF NOT EXISTS sports_picks_event_time
            ON sports_picks(event_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS sports_picks_caller_time
            ON sports_picks(caller_identity_id,created_at DESC);
        """
    )


def _migration_033_one_automatic_caller_name(db: DatabaseConnection) -> None:
    """Collapse the retired caller-ID picker to one automatic name per account."""

    rows = db.execute(
        """
        SELECT id,user_id FROM caller_identities
        WHERE user_id IS NOT NULL AND status='active'
        ORDER BY user_id,claimed_at,id
        """
    ).fetchall()
    canonical_by_user: dict[str, str] = {}
    retired_at = datetime.now(UTC).isoformat()
    for row in rows:
        user_id = str(row["user_id"])
        identity_id = str(row["id"])
        canonical_id = canonical_by_user.setdefault(user_id, identity_id)
        if identity_id == canonical_id:
            continue
        for table in ("community_calls", "signals", "sports_picks"):
            if "caller_identity_id" in _columns(db, table):
                db.execute(
                    f"UPDATE {table} SET caller_identity_id=? WHERE caller_identity_id=?",
                    (canonical_id, identity_id),
                )
        db.execute(
            "UPDATE caller_identity_claims SET caller_identity_id=NULL WHERE caller_identity_id=?",
            (identity_id,),
        )
        db.execute(
            """
            UPDATE caller_identities
            SET user_id=NULL,status='tombstoned',claim_cost_cents=NULL,
                payment_reference=NULL,claimed_at=NULL,deleted_at=?
            WHERE id=?
            """,
            (retired_at, identity_id),
        )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS caller_identities_one_active_per_user "
        "ON caller_identities(user_id) "
        "WHERE user_id IS NOT NULL AND status='active'"
    )


def _migration_034_sports_performance_history(db: DatabaseConnection) -> None:
    """Store completed box-score appearances for player performance history."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sports_player_appearances (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES sports_events(id) ON DELETE CASCADE,
            league TEXT NOT NULL,
            team_id TEXT NOT NULL,
            team_name TEXT NOT NULL,
            team_abbreviation TEXT NOT NULL,
            player_id TEXT NOT NULL,
            player_name TEXT NOT NULL,
            position TEXT NOT NULL DEFAULT '',
            starter INTEGER NOT NULL DEFAULT 0,
            won INTEGER NOT NULL CHECK(won IN (0,1)),
            stats_json TEXT NOT NULL DEFAULT '{}',
            collected_at TEXT NOT NULL,
            UNIQUE(event_id,player_id)
        );
        CREATE INDEX IF NOT EXISTS sports_player_league_history
            ON sports_player_appearances(league,player_id,event_id);
        CREATE INDEX IF NOT EXISTS sports_player_team_history
            ON sports_player_appearances(league,team_id,event_id);
        """
    )


def _migration_035_sports_news(db: DatabaseConnection) -> None:
    """Store team-linked news for promoted sports matchups."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sports_news_articles (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES sports_events(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            external_id TEXT NOT NULL,
            team_side TEXT NOT NULL CHECK(team_side IN ('home','away','both')),
            source_name TEXT NOT NULL,
            headline TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL,
            published_at TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            UNIQUE(event_id,provider,external_id)
        );
        CREATE INDEX IF NOT EXISTS sports_news_event_time
            ON sports_news_articles(event_id,published_at DESC);
        """
    )


def _migration_036_sports_request_indexes(db: DatabaseConnection) -> None:
    """Keep bounded slate and history reads fast across all leagues."""

    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS sports_events_start_league
            ON sports_events(start_time,league,completed);
        """
    )


def _sync_flash_version(db: DatabaseConnection) -> None:
    """Register one exact Flash release and reject silent configuration drift."""

    version = flash_version_snapshot(FLASH)
    existing = db.execute("SELECT * FROM flash_versions WHERE id=?", (version["id"],)).fetchone()
    timestamp = datetime.now(UTC).isoformat()
    if existing:
        row = dict(existing)
        if str(row["configuration_fingerprint"]) != version["configuration_fingerprint"]:
            raise RuntimeError(
                f"Flash version {version['id']!r} changed configuration. "
                "Set a new FLASH_VERSION_ID before starting the app."
            )
        db.execute(
            "UPDATE flash_versions SET status='active',retired_at=NULL WHERE id=?",
            (version["id"],),
        )
    else:
        db.execute(
            """
            INSERT INTO flash_versions(
                id,public_label,actor_id,status,provider,requested_model,
                allowed_resolved_model,prompt_version,context_version,
                risk_policy_version,output_schema_version,pipeline_version,
                forecast_contract_version,configuration_fingerprint,
                launched_at,created_at
            ) VALUES(?,?,?,'active',?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                version["id"],
                version["public_label"],
                version["actor_id"],
                version["provider"],
                version["requested_model"],
                version["allowed_resolved_model"],
                version["prompt_version"],
                version["context_version"],
                version["risk_policy_version"],
                version["output_schema_version"],
                version["pipeline_version"],
                version["forecast_contract_version"],
                version["configuration_fingerprint"],
                timestamp,
                timestamp,
            ),
        )
    db.execute(
        """
        UPDATE flash_versions SET status='retired',retired_at=COALESCE(retired_at,?)
        WHERE id<>? AND status='active'
        """,
        (timestamp, version["id"]),
    )


def _migration_037_flash_forecast_record(db: DatabaseConnection) -> None:
    """Add immutable, versioned Daily Flash forecasts and later market results."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS flash_versions (
            id TEXT PRIMARY KEY,
            public_label TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','retired')),
            provider TEXT NOT NULL,
            requested_model TEXT NOT NULL,
            allowed_resolved_model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            context_version TEXT NOT NULL,
            risk_policy_version TEXT NOT NULL,
            output_schema_version TEXT NOT NULL,
            pipeline_version TEXT NOT NULL,
            forecast_contract_version TEXT NOT NULL,
            configuration_fingerprint TEXT NOT NULL,
            launched_at TEXT NOT NULL,
            retired_at TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS flash_versions_status_time
            ON flash_versions(status,launched_at DESC);

        CREATE TABLE IF NOT EXISTS flash_forecasts (
            id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL UNIQUE
                REFERENCES research_commissions(id) ON DELETE CASCADE,
            version_id TEXT NOT NULL REFERENCES flash_versions(id),
            actor_snapshot_json TEXT NOT NULL,
            provider TEXT NOT NULL,
            requested_model TEXT NOT NULL,
            resolved_model TEXT NOT NULL,
            provider_request_id TEXT,
            ticker TEXT NOT NULL,
            exchange TEXT NOT NULL DEFAULT '',
            evidence_key TEXT NOT NULL,
            evidence_as_of TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('up','down','no_call')),
            probability_up REAL NOT NULL CHECK(probability_up>=0 AND probability_up<=1),
            reason TEXT NOT NULL,
            start_price REAL CHECK(start_price IS NULL OR start_price>0),
            start_at TEXT,
            price_source TEXT,
            market_session TEXT,
            contract_version TEXT NOT NULL,
            target_session_date TEXT,
            eligibility TEXT NOT NULL CHECK(eligibility IN ('eligible','unscored')),
            ineligibility_reason TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS flash_forecasts_version_time
            ON flash_forecasts(version_id,created_at DESC);
        CREATE INDEX IF NOT EXISTS flash_forecasts_ticker_time
            ON flash_forecasts(ticker,created_at DESC);

        CREATE TABLE IF NOT EXISTS flash_forecast_outcomes (
            forecast_id TEXT PRIMARY KEY REFERENCES flash_forecasts(id) ON DELETE CASCADE,
            status TEXT NOT NULL
                CHECK(status IN ('pending','resolved','no_call','void','under_review')),
            classification TEXT CHECK(classification IS NULL OR classification IN ('hit','miss')),
            miss_reason TEXT CHECK(
                miss_reason IS NULL OR miss_reason IN ('wrong_way','no_meaningful_move')
            ),
            end_price REAL CHECK(end_price IS NULL OR end_price>0),
            observed_at TEXT,
            return_pct REAL,
            signed_move_pct REAL,
            max_favorable_pct REAL,
            max_adverse_pct REAL,
            bar_source TEXT,
            bar_fingerprint TEXT,
            corporate_action_state TEXT,
            void_reason TEXT,
            first_checked_at TEXT,
            resolved_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS flash_forecast_outcomes_status_time
            ON flash_forecast_outcomes(status,updated_at);

        CREATE TABLE IF NOT EXISTS flash_evaluation_events (
            id TEXT PRIMARY KEY,
            forecast_id TEXT NOT NULL REFERENCES flash_forecasts(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL
                CHECK(event_type IN ('created','resolved','voided','reviewed','corrected')),
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS flash_evaluation_events_forecast_time
            ON flash_evaluation_events(forecast_id,created_at);
        """
    )
    _ensure_column(
        db,
        "research_commissions",
        "flash_version_id TEXT REFERENCES flash_versions(id)",
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS research_commissions_flash_version "
        "ON research_commissions(flash_version_id,created_at DESC)"
    )
    _sync_flash_version(db)


def _migration_038_sports_bookmaker_odds(db: DatabaseConnection) -> None:
    """Store each fresh sportsbook quote separately from the consensus receipt."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sports_bookmaker_odds (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES sports_events(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            sportsbook_key TEXT NOT NULL,
            sportsbook TEXT NOT NULL,
            market TEXT NOT NULL CHECK(market IN ('moneyline')),
            home_odds INTEGER NOT NULL CHECK(home_odds<>0),
            away_odds INTEGER NOT NULL CHECK(away_odds<>0),
            home_probability REAL NOT NULL,
            away_probability REAL NOT NULL,
            source_updated_at TEXT,
            quote_hash TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            UNIQUE(event_id,provider,sportsbook_key,quote_hash)
        );
        CREATE INDEX IF NOT EXISTS sports_bookmaker_odds_event_book_time
            ON sports_bookmaker_odds(event_id,sportsbook_key,observed_at DESC);
        CREATE INDEX IF NOT EXISTS sports_bookmaker_odds_event_source_time
            ON sports_bookmaker_odds(event_id,source_updated_at DESC);
        """
    )


def _migration_039_sports_ai_forecasts(db: DatabaseConnection) -> None:
    """Freeze and score pregame AI forecasts separately from the sports baseline."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sports_ai_forecasts (
            id TEXT PRIMARY KEY,
            report_id TEXT NOT NULL UNIQUE
                REFERENCES research_commissions(id) ON DELETE CASCADE,
            event_id TEXT NOT NULL REFERENCES sports_events(id) ON DELETE CASCADE,
            league TEXT NOT NULL CHECK(league IN ('mlb','nfl','nba','nhl')),
            actor_id TEXT NOT NULL,
            actor_snapshot_json TEXT NOT NULL,
            provider TEXT NOT NULL,
            requested_model TEXT NOT NULL,
            resolved_model TEXT NOT NULL,
            ladder_position INTEGER NOT NULL CHECK(ladder_position>=1),
            ladder_size INTEGER NOT NULL CHECK(ladder_size>=ladder_position),
            evidence_fingerprint TEXT NOT NULL,
            selection TEXT NOT NULL CHECK(selection IN ('home','away','pass')),
            home_probability REAL NOT NULL
                CHECK(home_probability>=0 AND home_probability<=1),
            away_probability REAL NOT NULL
                CHECK(away_probability>=0 AND away_probability<=1),
            confidence TEXT NOT NULL CHECK(confidence IN ('low','medium','high')),
            reason TEXT NOT NULL,
            contract_version TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            start_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open'
                CHECK(status IN ('open','settled','void')),
            result TEXT CHECK(result IS NULL OR result IN ('win','loss','pass','void')),
            brier_score REAL CHECK(brier_score IS NULL OR (brier_score>=0 AND brier_score<=1)),
            settled_at TEXT
        );
        CREATE INDEX IF NOT EXISTS sports_ai_forecasts_event_time
            ON sports_ai_forecasts(event_id,observed_at DESC);
        CREATE INDEX IF NOT EXISTS sports_ai_forecasts_model_league
            ON sports_ai_forecasts(actor_id,resolved_model,league,status,start_time);
        """
    )


def _migration_040_comment_glyph_avatars(db: DatabaseConnection) -> None:
    """Replace public comment emoji pairs with one abstract glyph per author and ticker."""

    migrate_comment_aliases_to_glyphs(db)


def _migration_041_persistent_comment_avatars(db: DatabaseConnection) -> None:
    """Give every account one durable public comment avatar and research ability."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS comment_avatars (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL UNIQUE,
            seed TEXT NOT NULL UNIQUE,
            ability_id TEXT NOT NULL CHECK(ability_id IN (
                'catalyst_scout','risk_sentinel','filing_sleuth',
                'pattern_mapper','liquidity_reader','countervoice'
            )),
            level INTEGER NOT NULL DEFAULT 1 CHECK(level>=1),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS comment_avatars_ability
            ON comment_avatars(ability_id,level);
        """
    )
    for row in db.execute("SELECT id FROM users WHERE status='active'").fetchall():
        ensure_comment_avatar(db, str(row["id"]))


def _migration_042_comment_generation_requests(db: DatabaseConnection) -> None:
    """Make paid AI comment requests safe to replay after a lost response."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS comment_generation_requests (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            idempotency_key_hash TEXT NOT NULL,
            ticker TEXT NOT NULL,
            comment_id TEXT UNIQUE REFERENCES ticker_comments(id) ON DELETE SET NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
            error_status INTEGER,
            error_detail TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id,idempotency_key_hash)
        );
        CREATE INDEX IF NOT EXISTS comment_generation_requests_status_time
            ON comment_generation_requests(status,updated_at);
        """
    )


def _migration_043_ranker_training_provenance(db: DatabaseConnection) -> None:
    """Identify replayed ranker rows without mixing them into public scan history."""

    _ensure_column(
        db,
        "ranker_training_examples",
        "training_origin TEXT NOT NULL DEFAULT 'live'",
    )
    _ensure_column(
        db,
        "ranker_training_examples",
        "provenance_json TEXT NOT NULL DEFAULT '{}'",
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS ranker_training_examples_origin_time
        ON ranker_training_examples(training_origin,captured_at DESC)
        """
    )


def _migration_044_golf_leaderboards(db: DatabaseConnection) -> None:
    """Store PGA tournaments separately from two-team sports matchups."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sports_golf_events (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            external_id TEXT NOT NULL,
            tour TEXT NOT NULL,
            name TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pre','in','post')),
            status_detail TEXT NOT NULL,
            completed INTEGER NOT NULL DEFAULT 0,
            venue TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL,
            first_collected_at TEXT NOT NULL,
            last_collected_at TEXT NOT NULL,
            UNIQUE(provider,tour,external_id)
        );
        CREATE INDEX IF NOT EXISTS sports_golf_events_time
            ON sports_golf_events(start_time,end_time,status);

        CREATE TABLE IF NOT EXISTS sports_golf_leaderboard (
            event_id TEXT NOT NULL
                REFERENCES sports_golf_events(id) ON DELETE CASCADE,
            player_id TEXT NOT NULL,
            player_name TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT '',
            position INTEGER,
            position_display TEXT NOT NULL,
            score REAL,
            score_display TEXT NOT NULL,
            through_display TEXT NOT NULL DEFAULT '',
            round_number INTEGER,
            round_display TEXT NOT NULL DEFAULT '',
            collected_at TEXT NOT NULL,
            PRIMARY KEY(event_id,player_id)
        );
        CREATE INDEX IF NOT EXISTS sports_golf_leaderboard_rank
            ON sports_golf_leaderboard(event_id,position,player_name);
        """
    )


def _migration_045_sports_comments(db: DatabaseConnection) -> None:
    """Store public, human-written game-thread comments."""

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sports_comments (
            id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES sports_events(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'public' CHECK(status IN ('public','hidden')),
            created_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user',
            generation_model TEXT
        );
        CREATE INDEX IF NOT EXISTS sports_comments_event_time
            ON sports_comments(event_id,status,created_at DESC);
        """
    )


def _migration_046_customer_llm_routing(db: DatabaseConnection) -> None:
    """Store private customer model routes and durable outbound edge work."""

    for definition in (
        "inference_scope TEXT NOT NULL DEFAULT 'managed'",
        "inference_route_json TEXT NOT NULL DEFAULT '{}'",
        "customer_inference INTEGER NOT NULL DEFAULT 0",
    ):
        _ensure_column(db, "research_commissions", definition)
    db.executescript(
        """
        DROP INDEX IF EXISTS research_commissions_daily_actor;
        CREATE UNIQUE INDEX IF NOT EXISTS research_commissions_daily_managed
            ON research_commissions(ticker,actor_id,report_day)
            WHERE report_day IS NOT NULL AND status IN ('running','complete')
                AND inference_scope='managed';
        CREATE UNIQUE INDEX IF NOT EXISTS research_commissions_daily_customer
            ON research_commissions(user_id,ticker,actor_id,report_day)
            WHERE report_day IS NOT NULL AND status IN ('running','complete')
                AND inference_scope<>'managed';

        CREATE TABLE IF NOT EXISTS llm_edge_connectors (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK(status IN ('active','revoked')),
            last_seen_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS llm_edge_connectors_user
            ON llm_edge_connectors(user_id,status,updated_at DESC);

        CREATE TABLE IF NOT EXISTS user_llm_routes (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            policy TEXT NOT NULL DEFAULT 'managed'
                CHECK(policy IN ('managed','prefer_customer','customer_only')),
            route_kind TEXT NOT NULL DEFAULT 'managed'
                CHECK(route_kind IN ('managed','edge')),
            model TEXT NOT NULL DEFAULT '',
            connector_id TEXT REFERENCES llm_edge_connectors(id) ON DELETE SET NULL,
            last_checked_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS llm_edge_jobs (
            id TEXT PRIMARY KEY,
            commission_id TEXT NOT NULL UNIQUE
                REFERENCES research_commissions(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            connector_id TEXT NOT NULL
                REFERENCES llm_edge_connectors(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','claimed','complete','failed')),
            model TEXT NOT NULL,
            request_json TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            response_json TEXT,
            claimed_at TEXT,
            lease_expires_at TEXT,
            completed_at TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS llm_edge_jobs_claim
            ON llm_edge_jobs(connector_id,status,created_at);
        CREATE INDEX IF NOT EXISTS llm_edge_jobs_commission
            ON llm_edge_jobs(commission_id,status);
        """
    )


def _migration_047_scorecard_and_release_indexes(db: DatabaseConnection) -> None:
    """Keep release and scorecard scans on their narrow lookup paths."""

    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS research_commissions_release_due
            ON research_commissions(exclusive_until)
            WHERE status='complete' AND report_day IS NOT NULL
              AND visibility<>'public' AND exclusive_until IS NOT NULL;
        CREATE INDEX IF NOT EXISTS research_commissions_flash_version_status
            ON research_commissions(flash_version_id,status)
            WHERE flash_version_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS kol_calls_predictor_exit_resolved
            ON kol_calls(predictor_id,exit_at,id)
            WHERE net_return_pct IS NOT NULL;
        """
    )


def _migration_048_shared_comment_subjects(db: DatabaseConnection) -> None:
    """Let the paid AI comment pipeline serve stocks and sports games."""

    for table in ("ticker_comments", "comment_generation_requests"):
        _ensure_column(
            db,
            table,
            "subject_kind TEXT NOT NULL DEFAULT 'stock' "
            "CHECK(subject_kind IN ('stock','sports_game'))",
        )
        _ensure_column(db, table, "subject_key TEXT NOT NULL DEFAULT ''")
        db.execute(f"UPDATE {table} SET subject_key=ticker WHERE subject_key='' ")
    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS ticker_comments_subject_time
            ON ticker_comments(subject_kind,subject_key,status,created_at DESC);
        CREATE INDEX IF NOT EXISTS comment_generation_requests_subject_time
            ON comment_generation_requests(subject_kind,subject_key,status,updated_at DESC);
        """
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
    Migration(26, "daily_report_alpha", _migration_026_daily_report_alpha),
    Migration(27, "gdpr_privacy", _migration_027_gdpr_privacy),
    Migration(28, "caller_identities", _migration_028_caller_identities),
    Migration(29, "signal_caller_identities", _migration_029_signal_caller_identities),
    Migration(30, "drop_passive_tracking", _migration_030_drop_passive_tracking),
    Migration(31, "scalable_scan_storage", _migration_031_scalable_scan_storage),
    Migration(32, "sports_domain", _migration_032_sports_domain),
    Migration(33, "one_automatic_caller_name", _migration_033_one_automatic_caller_name),
    Migration(34, "sports_performance_history", _migration_034_sports_performance_history),
    Migration(35, "sports_news", _migration_035_sports_news),
    Migration(36, "sports_request_indexes", _migration_036_sports_request_indexes),
    Migration(37, "flash_forecast_record", _migration_037_flash_forecast_record),
    Migration(38, "sports_bookmaker_odds", _migration_038_sports_bookmaker_odds),
    Migration(39, "sports_ai_forecasts", _migration_039_sports_ai_forecasts),
    Migration(40, "comment_glyph_avatars", _migration_040_comment_glyph_avatars),
    Migration(41, "persistent_comment_avatars", _migration_041_persistent_comment_avatars),
    Migration(42, "comment_generation_requests", _migration_042_comment_generation_requests),
    Migration(43, "ranker_training_provenance", _migration_043_ranker_training_provenance),
    Migration(44, "golf_leaderboards", _migration_044_golf_leaderboards),
    Migration(45, "sports_comments", _migration_045_sports_comments),
    Migration(46, "customer_llm_routing", _migration_046_customer_llm_routing),
    Migration(47, "scorecard_and_release_indexes", _migration_047_scorecard_and_release_indexes),
    Migration(48, "shared_comment_subjects", _migration_048_shared_comment_subjects),
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
        if unknown and not ALLOW_NEWER_DATABASE_SCHEMA:
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
            # Keep each PostgreSQL migration in its own transaction. A long
            # transaction can deadlock with live requests when a later
            # migration needs an exclusive table lock.
            if db.backend == "postgres":
                db.commit()
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
        _sync_flash_version(db)
        _seed_source_registry(db)


def main() -> None:
    """Apply the current database schema as a deployment release command."""

    try:
        init_db()
    finally:
        close_database_pool()
