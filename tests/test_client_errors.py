import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from runner_web import db
from runner_web import main as web_main
from runner_web.client_errors import client_error_summary, record_client_error
from runner_web.db import connection, init_db


def _client() -> TestClient:
    return TestClient(web_main.app, base_url=web_main.APP_ORIGIN)


def _prime(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "client-errors.db")
    init_db()


def test_client_errors_collapse_repeats_and_strip_page_query(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _prime(tmp_path, monkeypatch)
    seen_at = datetime(2026, 9, 15, 12, 30, tzinfo=UTC)

    first = record_client_error(
        kind="error",
        message="boom",
        source="app.js",
        line=10,
        column_number=2,
        stack="Error: boom\n  at app.js:10",
        page_url="https://runners.rati.chat/t/ONE?token=secret",
        user_agent="pytest-agent",
        client_ip="hashed-client",
        at=seen_at,
    )
    second = record_client_error(
        kind="error",
        message="boom",
        source="app.js",
        line=10,
        page_url="/t/ONE",
        client_ip="hashed-client",
        at=seen_at,
    )

    assert first == second
    summary = client_error_summary(at=seen_at)
    assert summary["unique_errors"] == 1
    assert summary["reports"] == 2
    error = summary["errors"][0]
    assert error["page_url"] == "/t/ONE"
    assert error["seen_count"] == 2
    assert error["kind"] == "error"


def test_client_error_endpoint_records_report(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _prime(tmp_path, monkeypatch)
    with _client() as client:
        response = client.post(
            "/api/client-errors",
            json={
                "kind": "rejection",
                "message": "unhandled rejection",
                "source": "https://runners.rati.chat/static/app.js",
                "line": 42,
                "page_url": "/pulse",
                "release": "abc123",
            },
            headers={"User-Agent": "pytest-browser"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "recorded"
    with connection() as database:
        row = database.execute(
            "SELECT kind,message,source,line,page_url,user_agent,release,seen_count "
            "FROM client_errors WHERE id=?",
            (payload["id"],),
        ).fetchone()
    assert row["kind"] == "rejection"
    assert row["message"] == "unhandled rejection"
    assert row["line"] == 42
    assert row["page_url"] == "/pulse"
    assert row["user_agent"] == "pytest-browser"
    assert row["release"] == "abc123"
    assert row["seen_count"] == 1


def test_client_error_endpoint_rejects_bad_reports(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _prime(tmp_path, monkeypatch)
    with _client() as client:
        blank = client.post("/api/client-errors", json={"message": ""})
        too_long = client.post("/api/client-errors", json={"message": "x" * 501})
        bad_kind = client.post(
            "/api/client-errors", json={"message": "boom", "kind": "k" * 41}
        )
        oversized = client.post(
            "/api/client-errors",
            json={"message": "boom", "padding": "x" * 20_100},
        )

    assert blank.status_code == 422
    assert too_long.status_code == 422
    assert bad_kind.status_code == 422
    assert oversized.status_code == 413


def test_client_error_summary_is_available_to_operations(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _prime(tmp_path, monkeypatch)
    record_client_error(
        kind="resource",
        message="SCRIPT failed to load: /static/missing.js",
        page_url="/roadmap",
        at=datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
    )

    from runner_web.operations import client_errors_api

    payload = client_errors_api(_access=None, limit=10)

    assert payload["unique_errors"] == 1
    assert payload["errors"][0]["message"].startswith("SCRIPT failed to load")
    assert json.dumps(payload)
