import sqlite3
from dataclasses import replace
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.ai_kol import FLASH
from runner_web.db import MIGRATIONS, connection, init_db


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
    assert [tuple(row) for row in migrations] == [
        (migration.version, migration.name) for migration in MIGRATIONS
    ]
    assert topic_table is not None
    assert pulse_state_table is not None
    assert reaction_table is not None
    assert comment_table is not None
    assert pseudonym_table is not None
    assert flash["slot"] == "flash"
    assert flash["ladder_position"] == 1
    assert flash["inference_model"] == "z-ai/glm-5.3"
    assert {"actor_id", "actor_snapshot_json"} <= commission_columns
    assert "actor_snapshot_json" in call_columns
    assert {
        "market_events_event_time",
        "market_events_ticker_event_time",
        "sec_filings_created",
        "sec_filings_ticker_filed_score",
        "scan_runs_candidate_captured",
        "scan_snapshots_ticker_captured",
        "ticker_reactions_ticker_reaction",
        "ticker_comments_ticker_time",
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
