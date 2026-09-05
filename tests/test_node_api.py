from __future__ import annotations

import json
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from runner_node.api import NodeService
from runner_node.app import create_app
from runner_node.config import NodeSettings
from runner_node.credentials import MemoryCredentialVault
from runner_node.openrouter import OpenRouterConnections
from runner_node.research import OpenRouterResearch
from runner_node.scans import ScanRequest, ScanStore


def _settings(**overrides: object) -> NodeSettings:
    values = {
        "mode": "local",
        "node_id": "test-node",
        "public_origin": "http://127.0.0.1:8787",
        "allowed_origins": ("tauri://localhost", "rati-app://app"),
        "allow_user_openrouter": True,
        "credential_backend": "memory",
        "auth_token": "test-node-token-with-24-characters",
        "database_path": None,
        **overrides,
    }
    return NodeSettings(**values)


def _client(
    *,
    settings: NodeSettings | None = None,
    vault: MemoryCredentialVault | None = None,
    exchange_code: object | None = None,
    research: OpenRouterResearch | None = None,
    cloud_source: object | None = None,
    remote_source: object | None = None,
    authenticated: bool = True,
) -> tuple[TestClient, MemoryCredentialVault]:
    settings = settings or _settings()
    vault = vault or MemoryCredentialVault()
    openrouter = OpenRouterConnections(
        vault,
        exchange_code=exchange_code or (lambda _code, _verifier: {"key": "sk-or-test-key"}),
    )
    service = NodeService(
        settings=settings,
        vault=vault,
        openrouter=openrouter,
        research=research,
        cloud_source=cloud_source,
        remote_source=remote_source,
    )
    client = TestClient(create_app(settings=settings, service=service))
    if authenticated and settings.auth_token:
        client.headers["Authorization"] = f"Bearer {settings.auth_token}"
    return client, vault


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
    assert "built-in-scanner" in providers
    assert "rati-cloud" in providers
    assert "openrouter" in providers
    assert providers["yahoo"]["state"] == "connected"
    assert providers["yahoo"]["configuration_kind"] == "none"
    assert providers["sec"]["configuration_kind"] == "none"
    assert providers["sec"]["configured"] is True
    assert providers["rati-cloud"]["state"] == "disabled"
    assert "OPENROUTER_API_KEY" not in response.text
    assert "credential_env" not in response.text


def test_coverage_is_grouped_by_scanner_capability() -> None:
    client, _vault = _client()

    response = client.get("/api/v1/coverage")

    assert response.status_code == 200
    payload = response.json()
    capabilities = {row["id"]: row for row in payload["capabilities"]}
    assert payload["summary"]["core_private_ready"] is True
    assert payload["summary"]["core_public_ready"] is False
    bars = capabilities["market_bars"]
    assert bars["selected_provider"] == "yahoo"
    providers = {provider["provider_id"]: provider for provider in bars["providers"]}
    assert providers["yahoo"]["access_model"] == "contract_review"
    assert "public_derived_signals" not in providers["yahoo"]["usage_rights"]
    assert providers["massive"]["configuration_kind"] == "api_key"


def test_provider_priority_is_saved_and_reported() -> None:
    vault = MemoryCredentialVault({"massive": "vault-massive-key"})
    client, _vault = _client(vault=vault)
    initial = {row["id"]: row for row in client.get("/api/v1/coverage").json()["capabilities"]}
    assert initial["market_bars"]["selected_provider"] == "massive"

    response = client.put(
        "/api/v1/routes/market_bars",
        json={"providers": ["yahoo"]},
    )

    assert response.status_code == 200
    assert response.json()["providers"] == ["yahoo", "massive"]
    capabilities = {row["id"]: row for row in client.get("/api/v1/coverage").json()["capabilities"]}
    assert capabilities["market_bars"]["selected_provider"] == "yahoo"
    assert "yahoo" in (vault.get("provider-routes") or "")


def test_provider_priority_rejects_wrong_capability() -> None:
    client, _vault = _client()

    response = client.put(
        "/api/v1/routes/market_bars",
        json={"providers": ["fintel"]},
    )

    assert response.status_code == 400
    assert "cannot provide" in response.json()["detail"]


def test_live_scan_runs_through_standalone_node(monkeypatch) -> None:
    def fake_scan(
        payload: ScanRequest,
        _provider_keys: dict[str, str],
        provider_routes: dict[str, list[str]],
    ) -> dict[str, object]:
        assert payload.top_n == 5
        assert payload.universe == "penny"
        assert payload.min_price == 0.2
        assert payload.max_price == 5
        assert provider_routes["market_bars"] == ["massive", "yahoo"]
        return {
            "status": "complete",
            "source": "live",
            "finished_at": "2026-01-01T00:00:00+00:00",
            "elapsed_seconds": 0.0,
            "rows": [],
            "warnings": [],
        }

    client, _vault = _client()
    monkeypatch.setattr("runner_node.api.run_scan", fake_scan)

    response = client.post("/api/v1/scans", json={"top_n": 5})

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"].startswith("scan_")
    assert payload["status"] == "complete"
    assert payload["source"] == "live"
    assert payload["rows"] == []
    assert client.get(f"/api/v1/scans/{payload['id']}").json() == payload
    assert client.get("/api/v1/scans").json()["receipts"] == [payload]


def test_sample_scan_input_is_rejected() -> None:
    client, _vault = _client()

    response = client.post("/api/v1/scans", json={"source": "sample"})

    assert response.status_code == 422


def test_local_ticker_detail_uses_scanner_provider_keys(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_detail(
        ticker: str,
        provider_keys: dict[str, str],
        provider_routes: dict[str, list[str]],
    ) -> dict[str, object]:
        captured.update(
            ticker=ticker,
            provider_keys=provider_keys,
            provider_routes=provider_routes,
        )
        return {
            "ticker": ticker,
            "source": "local_scanner",
            "quote": {"price": 1.23},
            "analysis": None,
            "charts": {"daily": [], "intraday": []},
            "pulls": [],
            "warnings": [],
        }

    vault = MemoryCredentialVault({"massive": "vault-massive-key"})
    client, _vault = _client(vault=vault)
    monkeypatch.setattr("runner_node.api.load_ticker_detail", fake_detail)

    response = client.get("/api/v1/tickers/AAPL")

    assert response.status_code == 200
    assert response.json()["source"] == "local_scanner"
    assert captured["ticker"] == "AAPL"
    assert captured["provider_keys"] == {"massive": "vault-massive-key"}
    assert captured["provider_routes"]["market_bars"] == ["massive", "yahoo"]


def test_cloud_node_rejects_local_ticker_detail() -> None:
    client, _vault = _client(settings=_settings(mode="cloud", allow_user_openrouter=False))

    response = client.get("/api/v1/tickers/AAPL")

    assert response.status_code == 403


def test_cloud_node_does_not_accept_user_triggered_scans() -> None:
    client, _vault = _client(settings=_settings(mode="cloud", allow_user_openrouter=False))

    response = client.post("/api/v1/scans", json={})

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
    assert status["connection_method"] == "stored"
    assert "key_fingerprint" not in status
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


@pytest.mark.parametrize("origin", ["tauri://localhost", "rati-app://app"])
def test_desktop_origin_receives_node_cors_headers(origin: str) -> None:
    client, _vault = _client()

    response = client.options(
        "/api/v1/node",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("origin", ["tauri://localhost", "http://tauri.localhost"])
def test_packaged_desktop_origins_reach_authenticated_scanner(monkeypatch, origin) -> None:
    monkeypatch.setenv("RATI_NODE_MODE", "local")
    monkeypatch.setenv("RATI_CREDENTIAL_BACKEND", "memory")
    monkeypatch.setenv("RATI_NODE_TOKEN", "test-node-token-with-24-characters")
    monkeypatch.delenv("RATI_NODE_ALLOWED_ORIGINS", raising=False)
    settings = NodeSettings.from_environment()
    client, _vault = _client(settings=settings)

    preflight = client.options(
        "/api/v1/scans",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    response = client.get("/api/v1/scans", headers={"Origin": origin})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin

    client.headers.pop("Authorization")
    assert client.get("/api/v1/scans", headers={"Origin": origin}).status_code == 401
    rejected = client.options(
        "/api/v1/scans",
        headers={
            "Origin": "http://tauri.localhost.evil.test",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers


def test_self_hosted_node_rejects_unauthenticated_writes() -> None:
    client, vault = _client(
        settings=_settings(mode="self_hosted"),
        authenticated=False,
    )

    assert client.post("/api/v1/scans", json={}).status_code == 401
    response = client.put(
        "/api/v1/connections/openrouter",
        json={"key": "sk-or-attacker-key-with-enough-characters"},
    )
    assert response.status_code == 401
    assert vault.get("openrouter") is None


def test_provider_key_is_stored_without_being_returned() -> None:
    client, vault = _client()

    response = client.put(
        "/api/v1/connections/massive",
        json={"key": "massive-secret-key"},
    )

    assert response.json() == {"provider": "massive", "status": "connected"}
    assert vault.get("massive") == "massive-secret-key"
    assert "massive-secret-key" not in response.text
    providers = {row["id"]: row for row in client.get("/api/v1/providers").json()["providers"]}
    assert providers["massive"]["configured"] is True


def test_rati_cloud_is_a_free_toggleable_source() -> None:
    class FakeCloudSource:
        def scans(self) -> list[dict[str, object]]:
            return [
                {
                    "id": "rati-one",
                    "source": "live",
                    "finished_at": "2026-08-30T00:00:00+00:00",
                    "rows": [],
                }
            ]

    client, vault = _client(cloud_source=FakeCloudSource())

    enabled = client.put("/api/v1/sources/rati-cloud", json={"enabled": True})
    pulled = client.get("/api/v1/source-scans")

    assert enabled.json() == {"source": "rati-cloud", "status": "connected"}
    assert vault.get("rati-cloud-enabled") == "enabled"
    assert pulled.json()["receipts"][0]["source_id"] == "rati-cloud"
    providers = {row["id"]: row for row in client.get("/api/v1/providers").json()["providers"]}
    assert providers["rati-cloud"]["enabled"] is True


def test_remote_scanner_is_saved_as_a_colored_source_without_exposing_token() -> None:
    captured: dict[str, str] = {}

    class FakeRemoteSource:
        def scans(self, url: str, token: str) -> list[dict[str, object]]:
            captured.update(url=url, token=token)
            return [
                {
                    "id": "remote-one",
                    "source": "live",
                    "finished_at": "2026-08-30T00:00:00+00:00",
                    "rows": [],
                }
            ]

    client, vault = _client(remote_source=FakeRemoteSource())
    response = client.post(
        "/api/v1/connections/scanners",
        json={
            "name": "Desk scanner",
            "url": "https://scanner.example.com/",
            "token": "private-remote-token",
        },
    )

    assert response.status_code == 200
    assert response.json()["url"] == "https://scanner.example.com"
    assert "private-remote-token" not in response.text
    scanner_id = response.json()["id"]
    providers = {row["id"]: row for row in client.get("/api/v1/providers").json()["providers"]}
    assert providers[f"remote:{scanner_id}"]["configuration_kind"] == "remote_scanner"
    receipts = client.get("/api/v1/source-scans").json()["receipts"]
    assert receipts[0]["source_name"] == "Desk scanner"
    assert captured == {
        "url": "https://scanner.example.com",
        "token": "private-remote-token",
    }
    assert "private-remote-token" in (vault.get("remote-scanners") or "")
    assert client.delete(f"/api/v1/connections/scanners/{scanner_id}").status_code == 200
    assert vault.get("remote-scanners") is None


def test_remote_scanner_rejects_insecure_non_loopback_address() -> None:
    client, _vault = _client()

    response = client.post(
        "/api/v1/connections/scanners",
        json={"name": "Unsafe", "url": "http://scanner.example.com", "token": ""},
    )

    assert response.status_code == 400
    assert "HTTPS" in response.json()["detail"]


def test_scan_receives_vault_backed_provider_keys(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_scan(
        _payload: object,
        provider_keys: dict[str, str],
        _provider_routes: dict[str, list[str]],
    ) -> dict[str, object]:
        captured.update(provider_keys)
        return {
            "status": "complete",
            "source": "live",
            "finished_at": "2026-01-01T00:00:00+00:00",
            "elapsed_seconds": 0.0,
            "rows": [],
            "warnings": [],
        }

    vault = MemoryCredentialVault({"massive": "vault-massive-key"})
    client, _vault = _client(vault=vault)
    monkeypatch.setattr("runner_node.api.run_scan", fake_scan)

    assert client.post("/api/v1/scans", json={}).status_code == 200
    assert captured == {"massive": "vault-massive-key"}


def test_openrouter_credential_powers_node_research() -> None:
    captured: dict[str, object] = {}

    def transport(key: str, payload: dict[str, object]) -> dict[str, object]:
        captured.update(key=key, payload=payload)
        return {
            "model": "test/research-model",
            "choices": [{"message": {"content": "Evidence first."}}],
        }

    vault = MemoryCredentialVault({"openrouter": "sk-or-research-key-with-enough-characters"})
    client, _vault = _client(
        vault=vault,
        research=OpenRouterResearch(transport),
    )

    response = client.post("/api/v1/research", json={"prompt": "Review ACME"})

    assert response.status_code == 200
    assert response.json()["answer"] == "Evidence first."
    assert captured["key"] == "sk-or-research-key-with-enough-characters"
    assert response.json()["model"] == "test/research-model"


def test_scan_store_persists_and_prunes_receipts(tmp_path) -> None:
    database_path = tmp_path / "scanner.sqlite3"
    first = ScanStore(maximum=2, database_path=database_path)
    saved = [
        first.save(
            {
                "source": "live",
                "finished_at": f"2026-01-0{day}T00:00:00+00:00",
                "rows": [],
            }
        )
        for day in range(1, 4)
    ]

    reopened = ScanStore(maximum=2, database_path=database_path)

    assert [receipt["id"] for receipt in reopened.list()] == [saved[2]["id"], saved[1]["id"]]
    assert reopened.get(saved[0]["id"]) is None


def test_scan_store_removes_legacy_non_live_receipts(tmp_path) -> None:
    database_path = tmp_path / "scanner.sqlite3"
    store = ScanStore(database_path=database_path)
    saved = store.save(
        {
            "source": "live",
            "finished_at": "2026-01-01T00:00:00+00:00",
            "rows": [],
        }
    )
    legacy = {**saved, "source": "sample"}
    with sqlite3.connect(database_path) as database:
        database.execute(
            "UPDATE node_scan_receipts SET payload_json=? WHERE id=?",
            (json.dumps(legacy), saved["id"]),
        )

    reopened = ScanStore(database_path=database_path)

    assert reopened.list() == []


def test_scan_store_rejects_non_live_receipts() -> None:
    store = ScanStore()

    with pytest.raises(ValueError, match="Only live"):
        store.save({"source": "sample", "rows": []})


def test_openrouter_flow_starts_are_rate_limited() -> None:
    client, _vault = _client()

    for _attempt in range(12):
        assert client.post("/api/v1/connections/openrouter/start").status_code == 200

    response = client.post("/api/v1/connections/openrouter/start")
    assert response.status_code == 429
    assert "Too many" in response.json()["detail"]


def test_self_hosted_mode_requires_a_long_token(monkeypatch) -> None:
    monkeypatch.setenv("RATI_NODE_MODE", "self_hosted")
    monkeypatch.delenv("RATI_NODE_TOKEN", raising=False)

    with pytest.raises(ValueError, match="RATI_NODE_TOKEN"):
        NodeSettings.from_environment()
