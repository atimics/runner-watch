import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db, operations
from runner_web import main as web_main
from runner_web.db import connection, init_db
from runner_web.operations import (
    TRAINER_HEARTBEAT_KEY,
    WORKER_HEARTBEAT_KEY,
    health_status,
    readiness_status,
    trainer_health,
    worker_health,
    worker_heartbeat_key,
)
from runner_web.performance import (
    performance_snapshot,
    record_cache,
    record_database_wait,
    record_route,
    reset_performance_metrics,
)


def test_performance_snapshot_reports_bounded_runtime_metrics() -> None:
    reset_performance_metrics()
    for duration in range(600):
        record_route("GET", "/api/t/{ticker}/chart", float(duration))
    record_cache("ticker-chart", "miss")
    record_cache("ticker-chart", "build", duration_ms=42.5)
    record_database_wait(3.25)

    snapshot = performance_snapshot()

    route = snapshot["routes"]["GET /api/t/{ticker}/chart"]
    assert route["requests"] == 600
    assert route["samples"] == 512
    assert route["p95_ms"] > route["p50_ms"]
    assert snapshot["caches"]["ticker-chart"]["miss"] == 1
    assert snapshot["caches"]["ticker-chart"]["build_ms"] == 42.5
    assert snapshot["database_pool_wait"]["p95_ms"] == 3.25
    assert snapshot["process"]["maximum_rss_mb"] > 0


def test_health_requires_a_fresh_worker_heartbeat(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
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


def test_health_rejects_an_old_worker_heartbeat(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
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


def test_health_reports_a_fresh_degraded_worker(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
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


def test_worker_contract_includes_kol_and_case_refreshers() -> None:
    required = operations.required_worker_names(sports_ingestion_enabled=False)

    assert "kol" in required
    assert "case-monitor" in required


def test_worker_health_recomputes_missing_required_names() -> None:
    checked_at = datetime(2026, 8, 25, 18, tzinfo=UTC)
    state = {
        worker_heartbeat_key("machine-a"): {
            "value": json.dumps(
                {
                    "status": "ok",
                    "required_workers": ["outcomes", "kol"],
                    "running_workers": ["outcomes"],
                }
            ),
            "updated_at": checked_at.isoformat(),
        }
    }

    result = worker_health(state, checked_at=checked_at)

    assert result["status"] == "degraded"
    assert result["detail"]["instances"][0]["missing_workers"] == ["kol"]


def test_worker_startup_schedules_kol_and_case_refreshers(
    monkeypatch: MonkeyPatch,
) -> None:
    class Task:
        def __init__(self, name: str) -> None:
            self.name = name

        def get_name(self) -> str:
            return self.name

        def done(self) -> bool:
            return False

    created: list[Task] = []

    def create_task(coroutine: object, *, name: str) -> Task:
        coroutine.close()
        task = Task(name)
        created.append(task)
        return task

    monkeypatch.setattr(web_main.asyncio, "create_task", create_task)
    monkeypatch.setattr(web_main, "SPORTS_INGESTION_ENABLED", False)

    web_main._start_worker_tasks()

    assert {task.name for task in created} >= {"kol", "case-monitor", "worker-heartbeat"}


def test_health_reports_one_stale_instance_when_another_is_fresh(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "multi-worker-health.db")
    init_db()
    checked_at = datetime(2026, 8, 25, 18, tzinfo=UTC)
    with connection() as database:
        for instance_id, age in (("machine-a", 30), ("machine-b", 180)):
            database.execute(
                "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
                (
                    worker_heartbeat_key(instance_id),
                    json.dumps(
                        {
                            "status": "ok",
                            "workers_running": 8,
                            "workers_expected": 8,
                            "failed_workers": [],
                        }
                    ),
                    (checked_at - timedelta(seconds=age)).isoformat(),
                ),
            )

    result = health_status(checked_at=checked_at)

    assert result["status"] == "degraded"
    assert result["worker"]["status"] == "degraded"
    assert result["worker"]["detail"]["instances_tracked"] == 2
    assert {item["status"] for item in result["worker"]["detail"]["instances"]} == {
        "ok",
        "stale",
    }


def test_health_requires_the_configured_number_of_worker_instances(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "expected-worker-health.db")
    monkeypatch.setattr(operations, "WORKER_EXPECTED_INSTANCES", 2)
    init_db()
    checked_at = datetime(2026, 8, 25, 18, tzinfo=UTC)
    with connection() as database:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
            (
                worker_heartbeat_key("machine-a"),
                json.dumps(
                    {
                        "status": "ok",
                        "workers_running": 8,
                        "workers_expected": 8,
                        "failed_workers": [],
                    }
                ),
                (checked_at - timedelta(seconds=30)).isoformat(),
            ),
        )

    result = health_status(checked_at=checked_at)

    assert result["status"] == "degraded"
    assert result["worker"]["status"] == "degraded"
    assert result["worker"]["detail"]["instances_expected"] == 2
    assert result["worker"]["detail"]["instances_fresh"] == 1


def test_readiness_does_not_depend_on_worker_heartbeat(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ready.db")
    init_db()

    result = readiness_status(checked_at=datetime(2026, 8, 25, 18, tzinfo=UTC))

    assert result["status"] == "ok"
    assert result["database"] == "ok"
    assert result["schema_version"] >= result["minimum_schema_version"]


def test_trainer_health_reports_running_and_stopped_states() -> None:
    checked_at = datetime(2026, 8, 29, 4, tzinfo=UTC)
    missing = trainer_health({}, checked_at=checked_at)
    assert missing["status"] == "stale"

    running = trainer_health(
        {
            TRAINER_HEARTBEAT_KEY: {
                "value": json.dumps({"status": "ok", "phase": "historical_backfill"}),
                "updated_at": (checked_at - timedelta(seconds=30)).isoformat(),
            }
        },
        checked_at=checked_at,
    )
    assert running["status"] == "ok"
    assert running["detail"]["phase"] == "historical_backfill"
