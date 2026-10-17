from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from runner_web import collection_retention as retention
from runner_web import db

AT = datetime(2026, 10, 17, tzinfo=UTC)


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "retention.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()


def run(run_id, days, *, status="ok", feed="test"):
    stamp = (AT - timedelta(days=days)).isoformat()
    with db.connection() as database:
        database.execute(
            "INSERT INTO ingestion_runs(id,source,feed,locator,status,started_at,finished_at,"
            "metadata_json) VALUES(?,'test',?,'local',?,?,?,?)",
            (run_id, feed, status, stamp, stamp, '{"received_count":1}'),
        )
        database.execute(
            "INSERT INTO ingestion_items(run_id,item_key,status,payload_json) "
            "VALUES(?,'item','accepted','{}')",
            (run_id,),
        )


def test_coverage_summary_keeps_totals_and_does_not_change_live_state():
    streams = [{"stream": f"wallet:{i}", "cursor": "x" * 200} for i in range(3000)]
    metadata = {"coverage": {"streams": streams, "event_counts": {"swap": 12}}, "received_count": 2}
    compact = retention.compact_metadata(metadata)
    assert len(json.dumps(compact)) < 400
    assert compact["coverage"]["stream_count"] == 3000
    assert compact["coverage"]["event_counts"] == {"swap": 12}
    assert len(compact["coverage"]["streams_sha256"]) == 64
    assert metadata["coverage"]["streams"] == streams
    assert retention.compact_metadata(compact) == compact


def test_large_metadata_has_a_bounded_summary():
    compact = retention.compact_metadata({"huge": "x" * 100000, "received_count": 12})
    assert len(json.dumps(compact)) < retention.MAX_METADATA_BYTES
    assert compact["received_count"] == 12
    assert compact["metadata_summary"] is True


def test_old_details_expire_and_summaries_preserve_provenance():
    run("expired", 31)
    run("recent", 1)
    result = retention.prune_collection_history(at=AT)
    assert result == {"detail_runs": 1, "items": 1, "summaries": 0}
    with db.connection() as database:
        old = database.execute("SELECT * FROM ingestion_runs WHERE id='expired'").fetchone()
        assert old["status"] == "ok" and old["details_pruned_at"] == AT.isoformat()
        assert database.execute("SELECT run_id FROM ingestion_items").fetchone()[0] == "recent"
    assert retention.prune_collection_history(at=AT)["detail_runs"] == 0


def test_run_budget_retires_oldest_details_in_small_batches(monkeypatch):
    monkeypatch.setattr(retention, "DETAIL_RUNS", 2)
    for i in range(5):
        run(str(i), i + 1)
    assert retention.prune_collection_history(at=AT, limit=1)["detail_runs"] == 1
    assert retention.prune_collection_history(at=AT)["detail_runs"] == 2
    with db.connection() as database:
        assert database.execute("SELECT count(*) FROM ingestion_items").fetchone()[0] == 2


def test_old_summary_keeps_coin_reference_and_latest_feed_status():
    run("unreferenced", 100)
    run("referenced", 99)
    run("latest", 98)
    with db.connection() as database:
        database.execute(
            "INSERT INTO memecoin_assets(coin_id,quote_json,collected_at,run_id) "
            "VALUES('coin','{}',?,'referenced')",
            (AT.isoformat(),),
        )
    retention.prune_collection_history(at=AT)
    with db.connection() as database:
        ids = {row[0] for row in database.execute("SELECT id FROM ingestion_runs").fetchall()}
    assert ids == {"referenced", "latest"}


def test_active_collection_keeps_its_details():
    run("active", 100, status="recording")
    assert retention.prune_collection_history(at=AT)["detail_runs"] == 0


def test_all_normalized_foreign_keys_are_covered_by_retention():
    with db.connection() as database:
        tables = [
            row[0]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        referenced = {
            table
            for table in tables
            for row in database.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            if row["table"] == "ingestion_runs"
        }
    assert referenced == set(retention.REFERENCE_TABLES) | {"ingestion_items"}


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"), reason="Requires a fresh test Postgres database"
)
def test_postgres_migration_retention_and_object_handoff(monkeypatch):
    import hashlib

    from runner_web import replay_objects, storage_maintenance
    from tests.test_replay_objects import FakeStore

    monkeypatch.setattr(db, "DATABASE_URL", os.environ["TEST_POSTGRES_URL"])
    db.init_db()
    run("old", 100)
    run("protected", 99)
    run("new", 1)
    with db.connection() as database:
        database.execute(
            "INSERT INTO memecoin_assets(coin_id,quote_json,collected_at,run_id) "
            "VALUES('pg-coin','{}',?,'protected')",
            (AT.isoformat(),),
        )
        database.execute(
            "INSERT INTO memecoin_replays(id,coin_id,payload_json,gif_bytes,gif_sha256,created_at) "
            "VALUES('pg-replay','pg-coin','{}',?,?,?)",
            (b"test gif", hashlib.sha256(b"test gif").hexdigest(), AT.isoformat()),
        )
    backend = FakeStore()
    monkeypatch.setenv("MEMECOIN_REPLAY_BUCKET", "test-replays")
    monkeypatch.setattr(replay_objects, "client", lambda: backend)
    assert storage_maintenance.migrate_gifs() == {"migrated": 1, "bytes": 8}
    assert retention.prune_collection_history(at=AT) == {
        "detail_runs": 2,
        "items": 2,
        "summaries": 1,
    }
    with db.connection() as database:
        assert database.execute("SELECT count(*) FROM ingestion_runs").fetchone()[0] == 2
        row = database.execute(
            "SELECT gif_bytes,gif_object_key,gif_sha256 FROM memecoin_replays"
        ).fetchone()
        assert bytes(row["gif_bytes"]) == b""
        assert replay_objects.read_gif(row["gif_object_key"], row["gif_sha256"]) == b"test gif"
    db.close_database_pool()
