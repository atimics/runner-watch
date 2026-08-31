import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch
from starlette.requests import Request

from runner_web import db, operations
from runner_web.db import connection, init_db


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
