from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from runner_node.api import NodeService
from runner_node.app import create_app
from runner_node.config import NodeSettings
from runner_node.credentials import MemoryCredentialVault
from runner_node.openrouter import OpenRouterConnections


def _settings(**overrides: object) -> NodeSettings:
    values = {
        "mode": "local",
        "node_id": "test-node",
        "public_origin": "http://127.0.0.1:8787",
        "allowed_origins": ("rati-app://app",),
        "allow_user_openrouter": True,
        "credential_backend": "memory",
        **overrides,
    }
    return NodeSettings(**values)  # type: ignore[arg-type]


def _client(
    *,
    settings: NodeSettings | None = None,
    vault: MemoryCredentialVault | None = None,
    exchange_code: object | None = None,
) -> tuple[TestClient, MemoryCredentialVault]:
    settings = settings or _settings()
    vault = vault or MemoryCredentialVault()
    openrouter = OpenRouterConnections(
        vault,
        exchange_code=exchange_code or (lambda _code, _verifier: {"key": "sk-or-test-key"}),
    )
    service = NodeService(settings=settings, vault=vault, openrouter=openrouter)
    return TestClient(create_app(settings=settings, service=service)), vault


def test_node_contract_reports_mode_and_capabilities() -> None:
    client, _vault = _client()

    response = client.get("/api/v1/node")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_version"] == "1"
    assert payload["node_id"] == "test-node"
    assert payload["mode"] == "local"
    assert payload["capabilities"]["stocks"] == "ready"
    assert payload["capabilities"]["research"] == "missing_connection"


def test_provider_contract_never_exposes_credentials() -> None:
    client, _vault = _client()

    response = client.get("/api/v1/providers")

    assert response.status_code == 200
    payload = response.json()
    providers = {provider["id"]: provider for provider in payload["providers"]}
    assert "sec" in providers
    assert "openrouter" in providers
    assert "OPENROUTER_API_KEY" not in response.text
    assert "credential_env" not in response.text


def test_sample_scan_runs_through_standalone_node() -> None:
    client, _vault = _client()

    response = client.post("/api/v1/scans", json={"source": "sample", "top_n": 5})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"].startswith("scan_")
    assert payload["status"] == "complete"
    assert payload["source"] == "sample"
    assert len(payload["rows"]) <= 5
    assert client.get(f"/api/v1/scans/{payload['id']}").json() == payload


def test_cloud_node_does_not_accept_user_triggered_scans() -> None:
    client, _vault = _client(settings=_settings(mode="cloud", allow_user_openrouter=False))

    response = client.post("/api/v1/scans", json={"source": "sample"})

    assert response.status_code == 403


def test_openrouter_pkce_exchange_keeps_key_out_of_api() -> None:
    captured: dict[str, str] = {}

    def exchange(code: str, verifier: str) -> dict[str, str]:
        captured.update(code=code, verifier=verifier)
        return {"key": "sk-or-v1-super-secret-desktop-key"}

    client, vault = _client(exchange_code=exchange)

    start = client.post("/api/v1/connections/openrouter/start")
    assert start.status_code == 200
    flow = start.json()
    authorization = urlparse(flow["authorization_url"])
    query = parse_qs(authorization.query)
    assert authorization.netloc == "openrouter.ai"
    assert query["code_challenge_method"] == ["S256"]
    assert query["callback_url"][0].startswith("http://127.0.0.1:8787/")
    assert "code_challenge" in query

    callback = client.get(
        f"/api/v1/connections/openrouter/callback/{flow['flow_id']}",
        params={"code": "one-use-code"},
    )
    assert callback.status_code == 200
    assert captured["code"] == "one-use-code"
    assert len(captured["verifier"]) >= 43
    assert vault.get("openrouter") == "sk-or-v1-super-secret-desktop-key"

    status = client.get("/api/v1/connections/openrouter").json()
    assert status["status"] == "connected"
    assert status["key_fingerprint"]
    assert "super-secret" not in str(status)
    assert (
        client.get(f"/api/v1/connections/openrouter/flows/{flow['flow_id']}").json()["status"]
        == "connected"
    )

    disconnected = client.delete("/api/v1/connections/openrouter").json()
    assert disconnected == {
        "status": "disconnected",
        "provider": "openrouter",
        "removed": True,
    }


def test_desktop_origin_receives_node_cors_headers() -> None:
    client, _vault = _client()

    response = client.options(
        "/api/v1/node",
        headers={
            "Origin": "rati-app://app",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "rati-app://app"
