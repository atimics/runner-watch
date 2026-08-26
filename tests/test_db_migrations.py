import sqlite3
from dataclasses import replace
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.ai_kol import FLASH
from runner_web.db import (
    MIGRATION_LOCK_ID,
    MIGRATIONS,
    _acquire_migration_lock,
    _release_migration_lock,
    connection,
    init_db,
)


class _LockResult:
    def fetchone(self) -> tuple[int]:
        return (1,)


class _PostgresLockDatabase:
    backend = "postgres"

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[int, ...]]] = []

    def execute(self, statement: str, parameters: tuple[int, ...]) -> _LockResult:
        self.calls.append((statement, parameters))
        return _LockResult()


def test_postgres_migrations_take_one_session_lock() -> None:
    database = _PostgresLockDatabase()

    _acquire_migration_lock(database)  # type: ignore[arg-type]
    _release_migration_lock(database)  # type: ignore[arg-type]

    assert database.calls == [
        ("SELECT pg_advisory_lock(?)", (MIGRATION_LOCK_ID,)),
        ("SELECT pg_advisory_unlock(?)", (MIGRATION_LOCK_ID,)),
    ]


def test_migrations_are_numbered_and_idempotent(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "migrations.db")

    init_db()
    init_db()

    with connection() as database:
        migrations = database.execute(
            "SELECT version,name FROM schema_migrations ORDER BY version"
        ).fetchall()
        topic_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='topic_snapshots'"
        ).fetchone()
        pulse_state_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pulse_profile_state'"
        ).fetchone()
        reaction_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ticker_reactions'"
        ).fetchone()
        comment_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ticker_comments'"
        ).fetchone()
        pseudonym_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='comment_pseudonyms'"
        ).fetchone()
        case_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='thesis_cases'"
        ).fetchone()
        revision_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='thesis_case_revisions'"
        ).fetchone()
        position_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_positions'"
        ).fetchone()
        update_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='thesis_case_updates'"
        ).fetchone()
        claim_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='evidence_claims'"
        ).fetchone()
        claim_source_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='evidence_claim_sources'"
        ).fetchone()
        research_stage_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='research_stage_runs'"
        ).fetchone()
        case_outcome_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='thesis_case_outcomes'"
        ).fetchone()
        position_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_positions'"
        ).fetchone()
        short_data_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='short_data_cache'"
        ).fetchone()
        stripe_event_table = database.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='stripe_webhook_events'"
        ).fetchone()
        user_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(users)").fetchall()
        }
        flash = database.execute("SELECT * FROM kol_predictors WHERE id=?", (FLASH.id,)).fetchone()
        commission_columns = {
            row["name"]
            for row in database.execute("PRAGMA table_info(research_commissions)").fetchall()
        }
        indexes = {
            row["name"]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        call_columns = {
            row["name"]
            for row in database.execute("PRAGMA table_info(kol_calls)").fetchall()
        }
        snapshot_columns = {
            row["name"]
            for row in database.execute("PRAGMA table_info(scan_snapshots)").fetchall()
        }
        case_columns = {
            row["name"]
            for row in database.execute("PRAGMA table_info(thesis_cases)").fetchall()
        }
        case_revision_columns = {
            row["name"]
            for row in database.execute("PRAGMA table_info(thesis_case_revisions)").fetchall()
        }
    assert [tuple(row) for row in migrations] == [
        (migration.version, migration.name) for migration in MIGRATIONS
    ]
    assert topic_table is not None
    assert pulse_state_table is not None
    assert reaction_table is not None
    assert comment_table is not None
    assert pseudonym_table is not None
    assert case_table is not None
    assert revision_table is not None
    assert position_table is not None
    assert update_table is not None
    assert claim_table is not None
    assert claim_source_table is not None
    assert research_stage_table is not None
    assert case_outcome_table is not None
    assert position_table is not None
    assert short_data_table is not None
    assert stripe_event_table is not None
    assert flash["slot"] == "flash"
    assert flash["ladder_position"] == 1
    assert flash["inference_provider"] == "openrouter"
    assert flash["inference_model"] == "z-ai/glm-5.3"
    assert {"actor_id", "actor_snapshot_json"} <= commission_columns
    assert {
        "case_id",
        "case_effect",
        "market_view",
        "model_confidence",
        "policy_version",
    } <= commission_columns
    assert "actor_snapshot_json" in call_columns
    assert {
        "opening_range_position",
        "support_distance_pct",
        "resistance_distance_pct",
        "fib_retracement_pct",
        "structure_available",
        "fibonacci_available",
        "short_interest_pct_float",
        "short_interest_shares",
        "days_to_cover",
        "short_interest_settlement_date",
        "borrow_fee_pct",
        "shares_available",
        "borrow_observed_at",
        "short_data_source",
        "short_data_url",
        "short_data_collected_at",
    } <= snapshot_columns
    assert {"source_kind", "source_comment_id", "horizon_minutes"} <= case_columns
    assert "source_comment_id" in case_revision_columns
    assert {
        "stripe_customer_id",
        "stripe_subscription_id",
        "stripe_subscription_status",
        "stripe_subscription_price_id",
        "stripe_current_period_end",
        "stripe_cancel_at_period_end",
        "billing_updated_at",
    } <= user_columns
    assert {
        "market_events_event_time",
        "market_events_ticker_event_time",
        "sec_filings_created",
        "sec_filings_ticker_filed_score",
        "scan_runs_candidate_captured",
        "scan_snapshots_ticker_captured",
        "ticker_reactions_ticker_reaction",
        "ticker_comments_ticker_time",
        "ticker_comments_public_time",
        "thesis_cases_user_status_time",
        "thesis_case_revisions_case_time",
        "thesis_case_updates_case_time",
        "thesis_cases_active_source_comment",
        "evidence_claims_ticker_collected",
        "evidence_claim_sources_url",
        "thesis_case_claims_claim",
        "research_stage_runs_commission_order",
        "research_stage_runs_model_time",
        "thesis_case_outcomes_status_due",
        "thesis_case_outcomes_ticker_due",
        "research_commissions_case_time",
        "research_commissions_policy_model",
        "user_positions_user_ticker_time",
        "user_positions_user_status_time",
        "short_data_cache_collected",
        "users_stripe_customer",
        "users_stripe_subscription",
        "stripe_webhook_events_type_time",
    } <= indexes


def test_baseline_migration_upgrades_a_legacy_database(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE users("
        "id TEXT PRIMARY KEY,username TEXT,display_name TEXT,status TEXT,created_at TEXT)"
    )
    legacy.commit()
    legacy.close()
    monkeypatch.setattr(db, "DATABASE_PATH", path)

    init_db()

    with connection() as database:
        user_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(users)").fetchall()
        }
        versions = database.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert "plan" in user_columns
    assert versions == len(MIGRATIONS)


def test_thesis_source_columns_repair_an_already_applied_migration(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    path = tmp_path / "legacy-thesis.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        );
        INSERT INTO schema_migrations(version,name,applied_at)
        VALUES(14,'thesis_cases','2026-08-25T00:00:00+00:00');
        CREATE TABLE thesis_cases (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE thesis_case_revisions (
            id TEXT PRIMARY KEY,
            case_id TEXT NOT NULL
        );
        """
    )
    legacy.close()
    monkeypatch.setattr(db, "DATABASE_PATH", path)

    init_db()

    with connection() as database:
        case_columns = {
            row["name"] for row in database.execute("PRAGMA table_info(thesis_cases)").fetchall()
        }
        revision_columns = {
            row["name"]
            for row in database.execute("PRAGMA table_info(thesis_case_revisions)").fetchall()
        }
    assert {"source_kind", "source_comment_id"} <= case_columns
    assert "source_comment_id" in revision_columns


def test_flash_keeps_its_identity_when_its_model_assignment_changes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "flash-model.db")
    init_db()
    replacement = replace(
        FLASH,
        model="future/model",
        description="Runner Watch's lead AI KOL, currently powered by a future model.",
    )
    monkeypatch.setattr(db, "FLASH", replacement)

    init_db()

    with connection() as database:
        flash = database.execute(
            "SELECT id,slot,ladder_position,inference_model FROM kol_predictors WHERE id=?",
            (FLASH.id,),
        ).fetchone()
    assert flash["id"] == "kol-flash"
    assert flash["slot"] == "flash"
    assert flash["ladder_position"] == 1
    assert flash["inference_model"] == "future/model"
