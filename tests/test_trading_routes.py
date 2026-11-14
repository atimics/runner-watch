import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from runner_trading import ledger
from runner_trading.config import Policy, stamp
from runner_trading.dashboard import snapshot
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


def test_coverage_separates_priced_missing_pending_and_actual_trades(client):
    at = datetime.now(UTC)
    paper = ledger.ensure_account("paper", Policy(), at)["id"]
    live = ledger.ensure_account("live", Policy(), at)["id"]
    cases = [
        ("a", "entered", -1, {"state": "quoted", "pnl_usdc": 1}, "1"),
        ("b", "skipped", -2, {"state": "unavailable", "reason": "No entry quote"}, None),
        ("a", "skipped", -3, {"state": "quoted", "pnl_usdc": -1}, "1"),
        ("b", "skipped", 1, None, None),
        ("c", "skipped", -1, None, None),
        ("c", "entry_failed", -2, {}, None),
    ]
    with connection() as db:
        for index, (mint, state, due_hours, outcome, units) in enumerate(cases):
            db.execute(
                "INSERT INTO trading_signals(id,account_id,mint,coin_id,observed_at,state,"
                "reason,evidence_json,baseline_units,outcome_due_at,outcome_at,outcome_json) "
                "VALUES(?,?,?,?,?,?,'Test decision','{}',?,?,?,?)",
                (
                    str(index),
                    paper,
                    mint,
                    mint,
                    stamp(at - timedelta(hours=8, minutes=index)),
                    state,
                    units,
                    stamp(at + timedelta(hours=due_hours)),
                    stamp(at) if outcome is not None else None,
                    json.dumps(outcome) if outcome is not None else None,
                ),
            )
        db.execute(
            "INSERT INTO trading_signals(id,account_id,mint,coin_id,observed_at,state,"
            "reason,evidence_json,outcome_due_at) "
            "VALUES('other-account',?,'other','other',?,'skipped','Other','{}',?)",
            (live, stamp(at), stamp(at)),
        )
        db.execute(
            "INSERT INTO trading_positions(id,account_id,signal_id,mint,coin_id,status,"
            "quantity_raw,cost_usdc,entry_fee_usdc,opened_at,closed_at,exit_usdc,pnl_usdc) "
            "VALUES('closed',?,'0','a','a','closed','1',25000000,1000,?,?,26000000,999000)",
            (paper, stamp(at - timedelta(hours=5)), stamp(at)),
        )
    cohort = snapshot("paper", at=at)["cohort"]
    assert cohort == {
        "total": 6,
        "distinct_tokens": 3,
        "skipped": 4,
        "entries_filled": 1,
        "entries_failed": 1,
        "entry_quoted": 2,
        "outcome_processed": 4,
        "outcome_priced": 2,
        "outcome_unavailable": 1,
        "outcome_pending": 2,
        "outcome_due": 5,
        "outcome_overdue": 1,
        "outcome_coverage_percent": 40.0,
        "outcome_unclassified": 1,
        "resolved": 4,
        "closed_trades": 1,
    }
    assert client.get("/api/trading").json()["cohort"] == cohort
    page = client.get("/trading").text
    assert "Priced outcomes" in page and "Unavailable outcomes" in page
    assert "Hold at 6 hours:" in page and "40.0%" in page


def test_empty_account_has_no_priced_coverage_percentage(client):
    ledger.ensure_account("paper", Policy(), datetime.now(UTC))
    cohort = client.get("/api/trading").json()["cohort"]
    assert cohort["total"] == cohort["outcome_processed"] == cohort["closed_trades"] == 0
    assert cohort["outcome_coverage_percent"] is None
    assert "Awaiting due checks" in client.get("/trading").text


def test_outcome_copy_uses_the_accounts_configured_holding_period(client):
    ledger.ensure_account("paper", Policy(max_hold_seconds=7200), datetime.now(UTC))
    assert "hypothetical hold of 2 hours" in client.get("/trading").text
