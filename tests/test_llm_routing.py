from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch
from starlette.requests import Request

from runner_web import db, edge_connector
from runner_web import main as web_main
from runner_web.ai_kol import FLASH
from runner_web.db import connection, init_db
from runner_web.flash_wallet import claim_daily_flash
from runner_web.llm_routing import LLMRouteError, normalize_openai_base_url, route_for_user
from runner_web.privacy import export_user_data


def _request(path: str, *, token: str | None = None) -> Request:
    headers = [(b"host", b"localhost:8080"), (b"origin", web_main.APP_ORIGIN.encode())]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 42000),
            "server": ("localhost", 8080),
        }
    )


def _create_user_and_session(user_id: str, raw_session: str) -> None:
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            (user_id, user_id, "Local Model User", "active", timestamp),
        )
        database.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (
                web_main.token_hash(raw_session),
                user_id,
                timestamp,
                (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            ),
        )


def test_cloud_endpoint_validation_rejects_local_and_plain_http(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "runner_web.llm_routing.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 0))],
    )

    with pytest.raises(LLMRouteError, match="HTTPS"):
        normalize_openai_base_url("http://models.example/v1")
    with pytest.raises(LLMRouteError, match="public internet address"):
        normalize_openai_base_url("https://models.example/v1")

    assert (
        normalize_openai_base_url("http://localhost:1234/v1/", allow_local=True)
        == "http://localhost:1234/v1"
    )


def test_route_policy_falls_back_only_when_the_user_chose_it(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "routes.db")
    init_db()
    _create_user_and_session("route-user", "route-session")
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            """
            INSERT INTO user_llm_routes(
                user_id,policy,route_kind,model,connector_id,created_at,updated_at
            ) VALUES(?,?,?, ?,NULL,?,?)
            """,
            (
                "route-user",
                "prefer_customer",
                "edge",
                "qwen3.5-27b",
                timestamp,
                timestamp,
            ),
        )
        preferred = route_for_user(database, "route-user", managed_model=FLASH.model)
        database.execute(
            "UPDATE user_llm_routes SET policy='customer_only' WHERE user_id=?",
            ("route-user",),
        )
        local_only = route_for_user(database, "route-user", managed_model=FLASH.model)

    assert preferred.kind == "managed"
    assert preferred.policy == "prefer_customer"
    assert preferred.customer_inference is False
    assert local_only.kind == "edge"
    assert local_only.available is False
    assert local_only.customer_inference is True


def test_customer_report_preparation_is_portable_and_fails_closed(
    monkeypatch: MonkeyPatch,
) -> None:
    def unexpected_cloud_call(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("customer route reached the managed cloud")

    monkeypatch.setattr(web_main.urllib.request, "urlopen", unexpected_cloud_call)
    prepared = web_main._generate_openrouter_report(
        "",
        {"ticker": "TEST", "company": "Test Company", "sources": []},
        "customer-user",
        model="qwen3.5-27b",
        customer_route=True,
        prepare_only=True,
    )

    assert isinstance(prepared, dict)
    assert prepared["model"] == "qwen3.5-27b"
    assert "plugins" not in prepared
    assert "provider" not in prepared
    assert "reasoning_effort" not in prepared
    with pytest.raises(web_main.ReportGenerationFailure) as failure:
        web_main._generate_openrouter_report(
            "",
            {"ticker": "TEST", "company": "Test Company", "sources": []},
            "customer-user",
            model="qwen3.5-27b",
            customer_route=True,
        )
    assert failure.value.diagnostics["phase"] == "edge_result_missing"


def test_local_connector_keeps_its_local_key_off_the_cloud(monkeypatch: MonkeyPatch) -> None:
    cloud_calls: list[tuple[str, dict[str, Any] | None]] = []

    def fake_cloud(
        _origin: str,
        path: str,
        _token: str,
        payload: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        cloud_calls.append((path, payload))
        if path.endswith("/claim"):
            return {
                "job": {
                    "id": "job-1",
                    "model": "qwen3.5-27b",
                    "request": {"model": "qwen3.5-27b", "messages": []},
                }
            }
        return {"ok": True}

    local_calls: list[dict[str, Any]] = []

    def fake_local(
        base_url: str,
        body: dict[str, Any],
        *,
        api_key: str | None,
        allow_local: bool,
        timeout: int,
    ) -> dict[str, Any]:
        local_calls.append(
            {
                "base_url": base_url,
                "body": body,
                "api_key": api_key,
                "allow_local": allow_local,
                "timeout": timeout,
            }
        )
        return {"model": "qwen3.5-27b", "choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(edge_connector, "_cloud_json", fake_cloud)
    monkeypatch.setattr(edge_connector, "call_chat_completions", fake_local)

    handled = edge_connector.run_once(
        origin="https://runners.rati.chat",
        token="connector-token",
        local_base_url="http://127.0.0.1:1234/v1",
        local_api_key="local-secret",
    )

    assert handled is True
    assert local_calls[0]["api_key"] == "local-secret"
    assert local_calls[0]["allow_local"] is True
    assert all("local-secret" not in json.dumps(payload or {}) for _, payload in cloud_calls)
    assert cloud_calls[-1][0].endswith("/complete")


def test_customer_model_report_cannot_be_published(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "private-report.db")
    monkeypatch.setattr(web_main, "enforce_rate", lambda *_args, **_kwargs: None)
    init_db()
    _create_user_and_session("private-user", "private-session")
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            """
            INSERT INTO research_commissions(
                id,public_id,user_id,ticker,evidence_key,status,requested_model,model,
                actor_id,headline,summary,created_at,updated_at,completed_at,
                report_day,visibility,inference_scope,inference_route_json,customer_inference
            ) VALUES(?,?,?,?,?,'complete',?,?,?,?,?,?,?,?,?,'private',?,?,1)
            """,
            (
                "private-report",
                "private-public-id",
                "private-user",
                "TEST",
                "evidence",
                "qwen3.5-27b",
                "qwen3.5-27b",
                FLASH.id,
                "Private report",
                "Private summary",
                timestamp,
                timestamp,
                timestamp,
                datetime.now(UTC).date().isoformat(),
                "customer:private-user",
                json.dumps({"kind": "edge", "model_identity": "self_reported"}),
            ),
        )

    with pytest.raises(HTTPException) as failure:
        web_main.publish_research_report_api(
            "private-public-id",
            _request("/api/research/private-public-id/publish"),
            "private-session",
        )
    assert failure.value.status_code == 409
    assert failure.value.detail == "Reports from your own model stay private."


def test_account_connector_can_claim_its_private_report_job(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "edge-job.db")
    monkeypatch.setattr(web_main, "enforce_rate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_main, "_community_engagement_count", lambda _ticker: 0)
    monkeypatch.setattr(
        web_main,
        "_alpha_evidence",
        lambda ticker, _engagement: (
            "edge-evidence",
            {"ticker": ticker, "company": "Edge Test", "sources": []},
        ),
    )
    monkeypatch.setattr(
        web_main,
        "prepare_forecast_evidence",
        lambda _ticker, evidence, **_kwargs: evidence,
    )
    monkeypatch.setattr(
        web_main,
        "build_research_context",
        lambda _ticker, evidence, **_kwargs: {
            **evidence,
            "context_stats": {"included_sections": 0},
        },
    )
    init_db()
    _create_user_and_session("edge-user", "edge-session")
    claim_daily_flash("edge-user")

    created_response = web_main.create_llm_connector_api(
        web_main.EdgeConnectorPayload(name="LM Studio"),
        _request("/api/account/llm-connectors"),
        "edge-session",
    )
    created = json.loads(created_response.body)
    connector_id = created["connector"]["id"]
    connector_token = created["token"]

    empty_claim = web_main.claim_edge_job_api(
        _request("/api/llm/edge/jobs/claim", token=connector_token)
    )
    assert json.loads(empty_claim.body) == {"job": None}

    web_main.update_account_llm_route_api(
        web_main.LLMRoutePayload(
            policy="customer_only",
            route_kind="edge",
            model="qwen3.5-27b",
            connector_id=connector_id,
        ),
        _request("/api/account/llm-route"),
        "edge-session",
    )
    settings_page = web_main.model_settings_page(
        _request("/settings/models"),
        "edge-session",
    )
    assert b"Run research in the cloud or on your computer" in settings_page.body
    commission, created_report = web_main._create_research_commission("edge-user", "EDGE")
    assert created_report is True
    queued = web_main._run_research_commission(str(commission["id"]))
    assert queued["status"] == "running"
    assert queued["customer_inference"] == 1

    claim_response = web_main.claim_edge_job_api(
        _request("/api/llm/edge/jobs/claim", token=connector_token)
    )
    job = json.loads(claim_response.body)["job"]
    assert job["model"] == "qwen3.5-27b"
    assert job["request"]["model"] == "qwen3.5-27b"
    assert "plugins" not in job["request"]
    with connection() as database:
        stored = database.execute(
            "SELECT * FROM llm_edge_jobs WHERE id=?",
            (job["id"],),
        ).fetchone()
    assert stored["status"] == "claimed"
    assert stored["request_fingerprint"] == job["request_fingerprint"]
    monkeypatch.setattr(
        web_main,
        "_generate_openrouter_report",
        lambda *_args, **_kwargs: (
            {
                "headline": "Private edge report",
                "thesis": "The local model returned a private draft.",
                "summary": "Private local-model summary.",
                "company_profile": {},
                "people": [],
                "filings": [],
                "catalysts": [],
                "risks": [],
                "watch": [],
                "unknowns": [],
                "sources": [],
                "citations": [],
                "forecast": {
                    "direction": "no_call",
                    "probability_up": 0.5,
                    "reason": "No directional edge.",
                },
            },
            "qwen3.5-27b",
            {},
        ),
    )
    completed_response = asyncio.run(
        web_main.complete_edge_job_api(
            job["id"],
            web_main.EdgeJobCompletePayload(response={"choices": []}),
            _request(f"/api/llm/edge/jobs/{job['id']}/complete", token=connector_token),
        )
    )
    completed = json.loads(completed_response.body)["report"]
    assert completed["status"] == "complete"
    with connection() as database:
        scorecard_entry = database.execute(
            "SELECT 1 FROM flash_forecasts WHERE report_id=?",
            (commission["id"],),
        ).fetchone()
        completed_row = database.execute(
            "SELECT * FROM research_commissions WHERE id=?",
            (commission["id"],),
        ).fetchone()
    assert scorecard_entry is None
    assert completed_row["visibility"] == "private"
    report_page = web_main.research_report_page(
        str(commission["public_id"]),
        _request(f"/research/{commission['public_id']}"),
        "edge-session",
    )
    assert b"Private local-model report" in report_page.body
    assert b"Publish now" not in report_page.body
    exported = export_user_data("edge-user")
    assert exported["model_routes"][0]["model"] == "qwen3.5-27b"
    assert exported["model_connectors"][0]["name"] == "LM Studio"
    assert "token_hash" not in json.dumps(exported["model_connectors"])
    revoked_response = web_main.revoke_llm_connector_api(
        connector_id,
        _request(f"/api/account/llm-connectors/{connector_id}"),
        "edge-session",
    )
    revoked = json.loads(revoked_response.body)
    assert revoked["route"]["policy"] == "managed"
    assert revoked["connectors"][0]["status"] == "revoked"
    with pytest.raises(HTTPException) as revoked_token:
        web_main.claim_edge_job_api(_request("/api/llm/edge/jobs/claim", token=connector_token))
    assert revoked_token.value.status_code == 401
