from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from runner_trading import ledger
from runner_trading.config import Policy, stamp
from runner_trading.engine import Trader
from runner_web import main as web
from runner_web.db import connection
from tests.test_trading import trading_db as trading_db


@pytest.fixture
def client(trading_db, monkeypatch):
    web.RATE_LIMITS.clear()
    monkeypatch.setattr(web, "APP_ORIGIN", "http://testserver")
    monkeypatch.setattr(web, "RUNNERS_ORIGIN", "http://testserver")
    monkeypatch.setenv("TRADING_OPERATOR_IDS", "operator")
    now = datetime.now(UTC)
    with connection() as db:
        for user_id in ("operator", "reader"):
            db.execute(
                "INSERT INTO users(id,username,display_name,status,created_at) "
                "VALUES(?,?,?,'active',?)",
                (user_id, user_id, user_id, stamp(now)),
            )
            db.execute(
                "INSERT INTO sessions(token_hash,user_id,created_at,expires_at,authenticated_at) "
                "VALUES(?,?,?,?,?)",
                (
                    web.token_hash(user_id),
                    user_id,
                    stamp(now),
                    stamp(now + timedelta(days=1)),
                    stamp(now),
                ),
            )
    return TestClient(web.app, base_url="http://testserver")


def test_public_dashboard_prepares_then_shows_paper_account(client):
    assert client.get("/trading").status_code == 200
    assert client.get("/api/trading").json()["state"] == "preparing"
    Trader(Policy(), source=lambda: []).cycle()
    result = client.get("/api/trading")
    assert result.json()["cash_usdc"] == 1_000_000_000
    assert result.json()["mode"] == "paper"
    assert "no-store" in result.headers["cache-control"]
    page = client.get("/trading")
    assert page.status_code == 200 and "Virtual funds" in page.text
    assert "$1,000.00" in page.text
    assert "Pause entries" not in page.text
    assert client.get("/health/trading").status_code == 200


def test_live_reads_and_controls_require_the_configured_operator(client):
    ledger.ensure_account("live", Policy(), datetime.now(UTC))
    ledger.ensure_account("paper", Policy(), datetime.now(UTC))
    assert client.get("/api/trading?mode=live").status_code == 401
    client.cookies.set("runner_session", "reader")
    assert client.get("/trading?mode=live").status_code == 403
    response = client.post(
        "/api/trading/control", json={"action": "pause"}, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 403
    client.cookies.set("runner_session", "operator")
    assert client.get("/trading?mode=live").status_code == 200
    bad = client.post(
        "/api/trading/control",
        json={"action": "pause"},
        headers={"Origin": "https://elsewhere.test"},
    )
    assert bad.status_code == 403
    good = client.post(
        "/api/trading/control", json={"action": "pause"}, headers={"Origin": "http://testserver"}
    )
    assert good.status_code == 200
    assert ledger.account("solana-paper-v1")["paused"]
    assert ledger.account("solana-live-v1")["paused"] == 0
    assert "Pause entries" not in client.get("/trading").text
    assert "Resume entries" in client.get("/trading").text


def test_controls_require_recent_auth_and_hide_signed_transactions(client):
    identifier = ledger.ensure_account("paper", Policy(), datetime.now(UTC))["id"]
    with connection() as db:
        db.execute("UPDATE sessions SET authenticated_at='2020-01-01T00:00:00+00:00'")
        db.execute(
            "INSERT INTO trading_orders(id,account_id,mint,side,status,input_raw,reason,"
            "created_at,updated_at,ready_at,signed_transaction,signature) "
            "VALUES('secret-order',?,'mint','sell','checking','1','Exit',?,?,?,'SECRET','SIG')",
            (identifier, stamp(), stamp(), stamp()),
        )
    client.cookies.set("runner_session", "operator")
    response = client.post(
        "/api/trading/control", json={"action": "resume"}, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 403
    assert "SECRET" not in client.get("/api/trading").text
