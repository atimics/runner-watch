from __future__ import annotations

import io
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from runner_node.scans import ScanStore
from runner_web import migrate_sqlite
from runner_web.database import open_database
from runner_web.sec_braid_transform import transform_braid_sec_stream
from tests.test_sec_braid_transform import (
    RUNNER_REVISION,
    SOURCE_MANIFEST_SHA256,
    SOURCE_RELEASE_ID,
    _stream,
)


@pytest.fixture
def sqlite_connections(monkeypatch):
    connections = []
    failure = {"statement": None}
    connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def execute(self, sql, parameters=()):
            if failure["statement"] and sql.strip().startswith(failure["statement"]):
                raise sqlite3.OperationalError("injected statement failure")
            return super().execute(sql, parameters)

        def close(self):
            super().close()
            self.closed = True

    def tracked_connect(*args, **kwargs):
        database = connect(*args, **kwargs, factory=TrackedConnection)
        connections.append(database)
        return database

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    yield connections, failure
    for database in connections:
        database.close()


def test_scan_store_closes_each_operation_and_commits_receipts(tmp_path, sqlite_connections):
    connections, _failure = sqlite_connections
    store = ScanStore(database_path=tmp_path / "scans.db")
    assert connections[-1].closed
    receipt = store.save({"source": "live", "rows": []})
    assert connections[-1].closed
    assert store.get(receipt["id"]) == receipt
    assert connections[-1].closed
    assert store.list() == [receipt]
    assert len(connections) == 4
    assert all(database.closed for database in connections)


@pytest.mark.parametrize("operation", ["initialize", "save", "get", "list"])
def test_scan_store_closes_failed_operations_and_rolls_back(
    tmp_path, sqlite_connections, operation
):
    connections, failure = sqlite_connections
    path = tmp_path / "scans.db"
    store = ScanStore(database_path=path)
    statements = {
        "initialize": "CREATE TABLE",
        "save": "DELETE FROM node_scan_receipts",
        "get": "SELECT payload_json",
        "list": "SELECT payload_json",
    }
    failure["statement"] = statements[operation]
    with pytest.raises(sqlite3.OperationalError, match="injected statement failure"):
        if operation == "initialize":
            ScanStore(database_path=path)
        elif operation == "save":
            store.save({"source": "live", "rows": []})
        elif operation == "get":
            store.get("missing")
        else:
            store.list()
    assert all(database.closed for database in connections)
    failure["statement"] = None
    assert store.list() == []


@pytest.mark.parametrize("invalid", [False, True])
def test_migration_closes_source_after_copy_or_target_rejection(
    tmp_path, monkeypatch, sqlite_connections, invalid
):
    connections, _failure = sqlite_connections
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    for path in (source_path, target_path):
        with closing(sqlite3.connect(path)) as database, database:
            database.execute("CREATE TABLE example(id INTEGER PRIMARY KEY,value TEXT)")
            if path == source_path or invalid:
                database.execute("INSERT INTO example VALUES(1,'kept')")
    monkeypatch.setattr(migrate_sqlite.schema, "DATABASE_URL", "")
    monkeypatch.setattr(migrate_sqlite.schema, "init_db", lambda: None)
    monkeypatch.setattr(
        migrate_sqlite, "open_database", lambda *_args: open_database("", target_path)
    )
    monkeypatch.setattr(migrate_sqlite, "_target_columns", lambda *_args: {"id", "value"})
    monkeypatch.setattr(migrate_sqlite, "_reset_sequences", lambda _target: None)

    if invalid:
        with pytest.raises(RuntimeError, match="Target table example is not empty"):
            migrate_sqlite.migrate(source_path, "postgresql://fixture")
    else:
        assert migrate_sqlite.migrate(source_path, "postgresql://fixture") == {"example": 1}
    assert all(database.closed for database in connections)
    with closing(sqlite3.connect(target_path)) as target:
        assert target.execute("SELECT * FROM example").fetchall() == [(1, "kept")]


@pytest.mark.parametrize("invalid", [False, True])
def test_braid_transform_closes_database_before_cleanup(
    tmp_path, monkeypatch, sqlite_connections, invalid
):
    connections, _failure = sqlite_connections
    unlink = Path.unlink

    def check_database_closed(path, *args, **kwargs):
        if path.name == "examples.sqlite3":
            assert connections[-1].closed
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", check_database_closed)
    output = tmp_path / "release"
    arguments = {
        "source_release_id": SOURCE_RELEASE_ID,
        "source_manifest_sha256": SOURCE_MANIFEST_SHA256,
        "runner_revision": RUNNER_REVISION,
    }
    if invalid:
        with pytest.raises(ValueError, match="not valid JSON"):
            transform_braid_sec_stream(io.StringIO("invalid JSON\n"), output, **arguments)
        assert list(tmp_path.iterdir()) == []
    else:
        manifest = transform_braid_sec_stream(io.StringIO(_stream()), output, **arguments)
        assert manifest["summary"]["filings"] == 4
        assert (output / "transform-release.json").exists()
        assert not (output / "examples.sqlite3").exists()
    assert len(connections) == 1
    assert connections[0].closed
