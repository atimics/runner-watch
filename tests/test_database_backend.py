from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from runner_web import database as database_module
from runner_web import db
from runner_web import main as web_main
from runner_web.database import initialize_sqlite, open_database, postgres_statement


def test_postgres_statement_converts_placeholders_and_sqlite_types() -> None:
    statement = postgres_statement(
        "INSERT OR IGNORE INTO samples(id,payload,note) VALUES(?,?, 'keep ? quoted');"
    )

    assert statement == (
        "INSERT INTO samples(id,payload,note) "
        "VALUES(%s,%s, 'keep ? quoted') ON CONFLICT DO NOTHING;"
    )
    assert "BYTEA" in postgres_statement("CREATE TABLE x (payload BLOB NOT NULL)")
    assert "BIGSERIAL PRIMARY KEY" in postgres_statement(
        "CREATE TABLE x (id INTEGER PRIMARY KEY AUTOINCREMENT)"
    )
    assert postgres_statement("SELECT MAX(score),MAX(score,?) FROM x") == (
        "SELECT MAX(score),GREATEST(score,%s) FROM x"
    )
    assert postgres_statement("SELECT MIN(score,COALESCE(?,0)) FROM x") == (
        "SELECT LEAST(score,COALESCE(%s,0)) FROM x"
    )
    assert (
        postgres_statement("INSERT INTO x(id,note) VALUES(:id,':keep') RETURNING id::text")
        == "INSERT INTO x(id,note) VALUES(%(id)s,':keep') RETURNING id::text"
    )


def test_public_daily_report_query_does_not_send_an_untyped_null_to_postgres(
    monkeypatch: MonkeyPatch,
) -> None:
    class EmptyResult:
        @staticmethod
        def fetchone() -> None:
            return None

    class RecordingDatabase:
        def __init__(self) -> None:
            self.queries: list[tuple[str, tuple[Any, ...]]] = []

        def execute(self, statement: str, parameters: tuple[Any, ...]) -> EmptyResult:
            self.queries.append((statement, parameters))
            return EmptyResult()

    database = RecordingDatabase()
    current_time = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)

    @contextmanager
    def recording_connection():
        yield database

    monkeypatch.setattr(web_main, "connection", recording_connection)
    monkeypatch.setattr(web_main, "_release_expired_daily_reports", lambda _database: 0)
    monkeypatch.setattr(web_main, "now", lambda: current_time)

    assert web_main.daily_report_for_ticker("TBLA") is None
    statement, parameters = database.queries[-1]
    assert "? IS NOT NULL" not in statement
    assert "CAST(? AS TEXT) IS NOT NULL" in statement
    assert "inference_scope='managed'" in statement
    assert parameters == (
        "TBLA",
        web_main.FLASH.id,
        "2026-08-30",
        None,
        None,
    )


def test_sqlite_backend_rows_support_names_and_positions(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    with open_database("", database_path) as database:
        database.execute("CREATE TABLE example(id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        database.execute("INSERT INTO example(id,name) VALUES(?,?)", (7, "Radar"))
        row = database.execute("SELECT id,name FROM example").fetchone()

    assert row is not None
    assert row[0] == 7
    assert row[:] == (7, "Radar")
    assert row["name"] == "Radar"
    assert dict(row) == {"id": 7, "name": "Radar"}


def test_sqlite_initialization_always_closes_its_connection(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    class RecordingConnection:
        def __init__(self) -> None:
            self.closed = False
            self.statements: list[str] = []

        def __enter__(self) -> RecordingConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str) -> None:
            self.statements.append(statement)

        def close(self) -> None:
            self.closed = True

    connection = RecordingConnection()
    monkeypatch.setattr(
        database_module.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    initialize_sqlite(tmp_path / "initialized.db")

    assert connection.closed is True
    assert connection.statements == [
        "PRAGMA busy_timeout=20000",
        "PRAGMA journal_mode=WAL",
    ]


def test_production_database_requires_an_encrypted_connection(
    monkeypatch: MonkeyPatch,
) -> None:
    assert db.database_tls_enabled("postgresql://db/app?sslmode=require") is True
    assert db.database_tls_enabled("postgresql://db/app?sslmode=verify-full") is True
    assert db.database_tls_enabled("postgresql://db/app") is False
    assert db.database_tls_enabled("postgresql://db/app?sslmode=disable") is False

    assert db.database_url_with_required_tls("postgresql://db/app") == (
        "postgresql://db/app?sslmode=require"
    )
    assert (
        db.database_url_with_required_tls("postgresql://db/app?application_name=runner")
        == "postgresql://db/app?application_name=runner&sslmode=require"
    )
    assert (
        db.database_url_with_required_tls("postgresql://db/app?sslmode=verify-full")
        == "postgresql://db/app?sslmode=verify-full"
    )

    monkeypatch.setattr(db, "DATABASE_URL", "postgresql://db/app?sslmode=disable")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_TLS", True)
    with pytest.raises(RuntimeError, match="must require TLS"):
        with db.connection():
            pass
