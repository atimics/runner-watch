import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.operations import WORKER_HEARTBEAT_KEY, health_status, readiness_status


def test_health_requires_a_fresh_worker_heartbeat(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "health.db")
    init_db()
    checked_at = datetime(2026, 8, 25, 18, tzinfo=UTC)

    missing = health_status(checked_at=checked_at)
    assert missing["status"] == "degraded"
    assert missing["worker"]["status"] == "stale"

    with connection() as database:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
            (
                WORKER_HEARTBEAT_KEY,
                json.dumps({"status": "ok", "workers_running": 10}),
                (checked_at - timedelta(seconds=30)).isoformat(),
            ),
        )

    healthy = health_status(checked_at=checked_at)
    assert healthy["status"] == "ok"
    assert healthy["worker"]["status"] == "ok"
    assert healthy["worker"]["age_seconds"] == 30.0


def test_health_rejects_an_old_worker_heartbeat(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "stale-health.db")
    init_db()
    checked_at = datetime(2026, 8, 25, 18, tzinfo=UTC)
    with connection() as database:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
            (
                WORKER_HEARTBEAT_KEY,
                "{}",
                (checked_at - timedelta(minutes=5)).isoformat(),
            ),
        )

    result = health_status(checked_at=checked_at)

    assert result["status"] == "degraded"
    assert result["worker"]["status"] == "stale"


def test_health_reports_a_fresh_degraded_worker(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "degraded-worker.db")
    init_db()
    checked_at = datetime(2026, 8, 25, 18, tzinfo=UTC)
    with connection() as database:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
            (
                WORKER_HEARTBEAT_KEY,
                json.dumps(
                    {
                        "status": "degraded",
                        "workers_running": 6,
                        "workers_expected": 7,
                        "failed_workers": ["outcomes"],
                    }
                ),
                (checked_at - timedelta(seconds=10)).isoformat(),
            ),
        )

    result = health_status(checked_at=checked_at)

    assert result["status"] == "degraded"
    assert result["worker"]["status"] == "degraded"
    assert result["worker"]["detail"]["failed_workers"] == ["outcomes"]


def test_readiness_does_not_depend_on_worker_heartbeat(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ready.db")
    init_db()

    result = readiness_status(checked_at=datetime(2026, 8, 25, 18, tzinfo=UTC))

    assert result["status"] == "ok"
    assert result["database"] == "ok"
    assert result["schema_version"] >= result["minimum_schema_version"]
