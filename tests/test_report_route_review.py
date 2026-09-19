"""Report buttons follow the same saved inference route as the commission API."""

import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from runner_web import main as web


@pytest.fixture
def routing(monkeypatch):
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript("""
        CREATE TABLE user_llm_routes (
            user_id TEXT, policy TEXT, route_kind TEXT, model TEXT, connector_id TEXT
        );
        CREATE TABLE llm_edge_connectors (
            id TEXT, user_id TEXT, status TEXT, last_seen_at TEXT
        );
    """)
    monkeypatch.setattr(web, "connection", lambda: nullcontext(database))
    provider = Mock(return_value=True)
    capacity = Mock(return_value=True)
    monkeypatch.setattr(web, "_flash_provider_ready", provider)
    monkeypatch.setattr(web, "_flash_daily_capacity_available", capacity)
    monkeypatch.setattr(web, "wallet_for_user", lambda user_id: {"balance": web.REPORT_COST})

    def save(policy, online=True):
        database.execute("DELETE FROM user_llm_routes")
        database.execute("DELETE FROM llm_edge_connectors")
        database.execute(
            "INSERT INTO user_llm_routes VALUES(?,?,?,?,?)",
            ("viewer", policy, "edge", "local-model", "connector"),
        )
        seen = datetime.now(UTC) - timedelta(seconds=0 if online else 600)
        database.execute(
            "INSERT INTO llm_edge_connectors VALUES(?,?,?,?)",
            ("connector", "viewer", "active", seen.isoformat()),
        )

    yield save, provider, capacity
    database.close()


def action(**changes):
    values = dict(
        user_id="viewer",
        latest_report=None,
        latest_attempt=None,
        start_url="/api/research/coin/example",
        login_url="/login",
    )
    values.update(changes)
    return web._flash_report_action(**values)


@pytest.mark.parametrize("policy", ["customer_only", "prefer_customer"])
@pytest.mark.parametrize(
    "provider_ready,capacity_ready", [(False, True), (True, False), (False, False)]
)
def test_available_edge_route_ignores_managed_outages(
    routing, policy, provider_ready, capacity_ready
):
    save, provider, capacity = routing
    save(policy)
    provider.return_value = provider_ready
    capacity.return_value = capacity_ready
    web._require_research_route("viewer")
    result = action()
    assert result["enabled"] is True
    assert result["state"] == "available"
    provider.assert_not_called()
    capacity.assert_not_called()


@pytest.mark.parametrize("policy", ["managed", "prefer_customer"])
@pytest.mark.parametrize(
    "provider_ready,capacity_ready,detail",
    [
        (False, True, "Try again later"),
        (True, False, "Daily limit reached"),
        (True, True, None),
    ],
)
def test_managed_route_and_customer_fallback_use_managed_limits(
    routing, policy, provider_ready, capacity_ready, detail
):
    save, provider, capacity = routing
    save(policy, online=False)
    provider.return_value = provider_ready
    capacity.return_value = capacity_ready
    result = action()
    assert result["enabled"] is (detail is None)
    if detail:
        assert result["detail"] == detail
    assert provider.call_count == 1
    assert capacity.call_count == int(provider_ready)


def test_customer_only_offline_matches_api_route_error(routing):
    save, provider, capacity = routing
    save("customer_only", online=False)
    with pytest.raises(HTTPException) as error:
        web._require_research_route("viewer")
    result = action()
    assert result["enabled"] is False
    assert result["state"] == "unavailable"
    assert result["detail"] == error.value.detail == "Start your local model connector."
    provider.assert_not_called()
    capacity.assert_not_called()


@pytest.mark.parametrize(
    "report,state",
    [
        ({"locked": True}, "locked"),
        ({"status": "running", "public_id": "job"}, "running"),
        ({"status": "complete", "public_id": "saved", "visibility": "private"}, "complete"),
    ],
)
def test_saved_report_access_precedes_route_availability(routing, report, state):
    save, provider, capacity = routing
    save("customer_only", online=False)
    result = action(latest_report=report)
    assert result["state"] == state
    if state == "complete":
        assert result["href"] == "/research/saved"
    elif state == "running":
        assert result["job_id"] == "job"
    provider.assert_not_called()
    capacity.assert_not_called()


def test_edge_route_keeps_wallet_gate_and_failed_retry(routing, monkeypatch):
    save, _, _ = routing
    save("customer_only")
    monkeypatch.setattr(web, "wallet_for_user", lambda _: {"balance": 0})
    assert action()["state"] == "insufficient"
    monkeypatch.setattr(web, "wallet_for_user", lambda _: {"balance": web.REPORT_COST})
    result = action(latest_attempt={"status": "failed", "report_day": web.now().date().isoformat()})
    assert result["enabled"] is True
    assert result["state"] == "failed"
    assert result["message"] == web.FLASH_REPORT_FAILED_MESSAGE


def test_signed_out_keeps_login_action(routing):
    result = action(user_id=None)
    assert result["state"] == "login"
    assert result["href"] == "/login"
