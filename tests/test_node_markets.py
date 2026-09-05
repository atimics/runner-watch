from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from runner_node.api import NodeService
from runner_node.app import create_app
from runner_node.cloud_source import RATI_CLOUD_ORIGIN
from runner_node.config import NodeSettings
from runner_node.credentials import MemoryCredentialVault


@pytest.fixture
def client():
    settings = NodeSettings(
        mode="local",
        node_id="market-test",
        public_origin="http://127.0.0.1:8787",
        allowed_origins=(),
        allow_user_openrouter=True,
        credential_backend="memory",
        auth_token="private-local-node-token",
        database_path=None,
    )
    service = NodeService(settings=settings, vault=MemoryCredentialVault())
    with TestClient(create_app(settings=settings, service=service)) as value:
        value.headers["Authorization"] = f"Bearer {settings.auth_token}"
        yield value


def enable(client):
    assert client.put("/api/v1/sources/rati-cloud", json={"enabled": True}).status_code == 200


def test_market_routes_require_connection_and_node_access(client, monkeypatch):
    requests = []
    monkeypatch.setattr("runner_node.cloud_source._get_json", lambda url: requests.append(url))
    paths = [
        "/api/v1/markets/memecoins",
        "/api/v1/markets/memecoins/calls",
        "/api/v1/markets/memecoins/coins/dogecoin",
        "/api/v1/markets/sports/pulse",
    ]
    for path in paths:
        assert client.get(path).status_code == 409
    enable(client)
    client.headers.pop("Authorization")
    for path in paths:
        assert client.get(path).status_code == 401
    assert requests == []


def test_market_coverage_follows_callable_routes(client):
    def coverage():
        return {row["id"]: row for row in client.get("/api/v1/coverage").json()["capabilities"]}

    for key in ("memecoins", "sports"):
        assert client.get("/api/v1/node").json()["capabilities"][key] == "missing_connection"
    for key in ("crypto_markets", "sports_scores", "sports_odds", "sports_news"):
        assert coverage()[key]["private_ready"] is False
    coingecko = next(
        row
        for row in client.get("/api/v1/providers").json()["providers"]
        if row["id"] == "coingecko"
    )
    assert coingecko["runtime_available"] is False
    assert coingecko["state"] == "cloud_required"
    enable(client)
    for key in ("crypto_markets", "sports_scores"):
        row = coverage()[key]
        assert row["private_ready"] is True
        assert row["selected_provider"] == "rati-cloud"
    assert coverage()["sports_odds"]["private_ready"] is False
    for key in ("memecoins", "sports"):
        assert client.get("/api/v1/node").json()["capabilities"][key] == "ready"
    client.put("/api/v1/sources/rati-cloud", json={"enabled": False})
    assert coverage()["crypto_markets"]["private_ready"] is False


def test_memecoin_query_is_encoded_and_local_credentials_stay_local(client, monkeypatch):
    captured = []

    def get_json(url, token=""):
        captured.append((url, token))
        return {"rows": [{"id": "dogecoin"}], "status": "stale", "refresh_failed": True}

    monkeypatch.setattr("runner_node.cloud_source._get_json", get_json)
    enable(client)
    response = client.get(
        "/api/v1/markets/memecoins", params={"q": "doge & coin", "sort": "gainers"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "stale"
    assert response.json()["refresh_failed"] is True
    url, token = captured[0]
    assert url.startswith(f"{RATI_CLOUD_ORIGIN}/api/memecoins?")
    assert parse_qs(urlparse(url).query) == {"q": ["doge & coin"], "sort": ["gainers"]}
    assert token == ""


@pytest.mark.parametrize(
    ("local_path", "cloud_path", "payload"),
    [
        ("memecoins/coins/radar", "memecoins/radar", {"coin": {"id": "radar"}}),
        ("memecoins/calls", "memecoin-calls", {"calls": [{"coin_id": "dogecoin"}]}),
        ("sports/pulse", "sports/pulse?limit=40", {"events": [{"id": "game-1"}]}),
        ("sports/radar", "sports/radar?limit=40", {"events": [{"id": "game-1"}]}),
        ("sports/alpha", "sports/alpha?limit=40", {"rows": [{"event_id": "game-1"}]}),
    ],
)
def test_public_market_views_have_fixed_upstream_routes(
    client, monkeypatch, local_path, cloud_path, payload
):
    captured = []

    def get_json(url):
        captured.append(url)
        return payload

    monkeypatch.setattr("runner_node.cloud_source._get_json", get_json)
    enable(client)
    response = client.get(f"/api/v1/markets/{local_path}")
    assert response.status_code == 200
    assert response.json() == payload
    assert captured == [f"{RATI_CLOUD_ORIGIN}/api/{cloud_path}"]


def test_market_requests_reject_invalid_views_and_coin_ids(client, monkeypatch):
    captured = []
    monkeypatch.setattr("runner_node.cloud_source._get_json", lambda url: captured.append(url))
    enable(client)
    assert client.get("/api/v1/markets/memecoins?sort=unknown").status_code == 422
    assert client.get("/api/v1/markets/memecoins", params={"q": "x" * 81}).status_code == 422
    assert client.get("/api/v1/markets/memecoins/coins/INVALID").status_code == 400
    assert client.get("/api/v1/markets/sports/unknown").status_code == 422
    assert captured == []


@pytest.mark.parametrize(
    "payload", [{"rows": {}}, {"rows": [{}]}, {"rows": [{"id": "doge"}] * 101}]
)
def test_malformed_market_feeds_return_a_source_error(client, monkeypatch, payload):
    monkeypatch.setattr("runner_node.cloud_source._get_json", lambda _url: payload)
    enable(client)
    assert client.get("/api/v1/markets/memecoins").status_code == 502


def test_coin_identity_and_upstream_failure_are_checked(client, monkeypatch):
    monkeypatch.setattr(
        "runner_node.cloud_source._get_json", lambda _url: {"coin": {"id": "other"}}
    )
    enable(client)
    assert client.get("/api/v1/markets/memecoins/coins/dogecoin").status_code == 502

    def unavailable(_url):
        raise RuntimeError("RATi Cloud is unavailable")

    monkeypatch.setattr("runner_node.cloud_source._get_json", unavailable)
    response = client.get("/api/v1/markets/sports/pulse")
    assert response.status_code == 502
    assert response.json()["detail"] == "RATi Cloud is unavailable"
