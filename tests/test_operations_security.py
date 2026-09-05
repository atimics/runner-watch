import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch
from starlette.requests import Request

from runner_web import db, operations
from runner_web.db import connection, init_db
from runner_web.main import app


def request(token: str = "") -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "method": "GET", "path": "/health/details", "headers": headers})


def test_detailed_operations_require_a_bearer_token(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(operations, "OPERATIONS_TOKEN", "operator-secret")

    with pytest.raises(HTTPException) as missing:
        operations.require_operations_access(request())
    assert missing.value.status_code == 404

    with pytest.raises(HTTPException) as wrong:
        operations.require_operations_access(request("wrong-secret"))
    assert wrong.value.status_code == 404

    operations.require_operations_access(request("operator-secret"))


def test_every_private_operations_route_requires_the_bearer_token() -> None:
    private_paths = {
        "/health/details",
        "/health/data/details",
        "/health/performance",
        "/api/ranker/status",
        "/api/ingestion/status",
        "/api/capabilities",
    }
    protected_paths = {
        route.path
        for route in operations.router.routes
        if any(
            dependency.call is operations.require_operations_access
            for dependency in route.dependant.dependencies
        )
    }

    assert private_paths <= protected_paths

    intelligence_route = next(
        route for route in app.routes if getattr(route, "path", None) == "/api/intelligence"
    )
    assert any(
        dependency.call is operations.require_operations_access
        for dependency in intelligence_route.dependant.dependencies
    )


def test_public_readiness_response_is_minimal(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        operations,
        "readiness_status",
        lambda: {
            "status": "ok",
            "database": "ok",
            "schema_version": 42,
            "minimum_schema_version": 42,
        },
    )

    response = operations.readiness_api()

    assert json.loads(response.body) == {"status": "ok"}


@pytest.mark.parametrize(
    ("database", "worker", "trainer", "expected"),
    [
        ("ok", "ok", "ok", 200),
        ("unavailable", "ok", "ok", 503),
        ("ok", "stale", "ok", 503),
        ("ok", "degraded", "ok", 503),
        ("ok", "ok", "stale", 503),
        ("ok", "ok", "degraded", 503),
    ],
)
def test_public_worker_health_checks_each_process_and_returns_only_status(
    monkeypatch: MonkeyPatch, database: str, worker: str, trainer: str, expected: int
) -> None:
    monkeypatch.setattr(
        operations,
        "health_status",
        lambda: {
            "database": database,
            "worker": {"status": worker, "detail": {"instance_id": "private-machine"}},
            "trainer": {"status": trainer, "detail": {"model_id": "private-model"}},
        },
    )

    response = operations.workers_health_api()

    assert response.status_code == expected
    assert json.loads(response.body) == {"status": "ok" if expected == 200 else "degraded"}


def test_detailed_health_never_returns_raw_worker_errors(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "operations.db")
    init_db()
    with connection() as database:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
            (
                "background_scan_last_error",
                "secret-bearing upstream URL must not leave the server",
                "2026-08-30T12:00:00+00:00",
            ),
        )

    payload = operations.health_status()

    assert payload["scan_error"] is True
    assert "secret-bearing" not in json.dumps(payload)
