from __future__ import annotations

from pathlib import Path

from runner_web.database import open_database, postgres_statement


def test_postgres_statement_converts_placeholders_and_sqlite_types() -> None:
    statement = postgres_statement(
        "INSERT OR IGNORE INTO samples(id,payload,note) "
        "VALUES(?,?, 'keep ? quoted');"
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
    assert postgres_statement(
        "INSERT INTO x(id,note) VALUES(:id,':keep') RETURNING id::text"
    ) == "INSERT INTO x(id,note) VALUES(%(id)s,':keep') RETURNING id::text"


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
