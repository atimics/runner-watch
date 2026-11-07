from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from solders.keypair import Keypair

from runner_trading import ledger, proposals
from runner_trading.config import Policy, stamp
from runner_trading.providers import ProviderError
from runner_trading.wallet import create as create_wallet
from runner_web import dash_trading
from runner_web.db import connection
from tests.test_trading import MINT, FakeJupiter, candidate
from tests.test_trading_live import execution, reserve
from tests.test_trading_routes import client as client
from tests.test_trading_routes import trading_db as trading_db


def fund(monkeypatch):
    now = datetime.now(UTC)
    wallet = str(Keypair().pubkey())
    monkeypatch.setenv("TRADING_WALLET", wallet)
    ledger.ensure_account("live", Policy(), now)
    with connection() as db:
        db.execute(
            "UPDATE trading_accounts SET cash_usdc=1000000000,sol_lamports=50000000,"
            "peak_equity_usdc=1000000000,day_equity_usdc=1000000000,heartbeat_at=?,"
            "health_json=?,wallet=? WHERE id=?",
            (stamp(now), '{"state":"ok"}', wallet, proposals.ACCOUNT),
        )
        db.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES('memecoins_snapshot',?,?)",
            (ledger.encode({"rows": [candidate(now)]}), stamp(now)),
        )
    return now, wallet


def idea(now, *, source="test", amount=250_000_000):
    return proposals.create(
        source=source,
        mint=MINT,
        side="buy",
        amount=amount,
        thesis="Pool demand is improving.",
        invalidation="Liquidity falls or holders concentrate.",
        at=now,
    )


def review(identifier, now):
    return proposals.preview(
        identifier,
        provider=FakeJupiter(lambda: now),
        rpc=lambda *_: {"value": {"decimals": 6}},
        clock=lambda: now,
    )


def test_deposits_set_live_budget_once_and_withdrawal_needs_review(trading_db):
    live, _, chain, _ = execution()
    identifier = ledger.ensure_account("live", Policy(), datetime.now(UTC))["id"]
    assert ledger.account(identifier)["cash_usdc"] == 0
    from tests.test_trading import AT

    token = ledger.acquire(identifier, AT)
    live.reconcile(identifier, token, AT)
    live.reconcile(identifier, token, AT)
    acc = ledger.account(identifier)
    assert (acc["cash_usdc"], acc["peak_equity_usdc"], acc["day_equity_usdc"]) == (
        1_000_000_000,
    ) * 3
    with connection() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM trading_ledger WHERE kind='funding'").fetchone()[0]
            == 1
        )
        db.execute("UPDATE trading_accounts SET cash_usdc=cash_usdc+1")
    live.reconcile(identifier, token, AT)
    assert ledger.account(identifier)["paused"] == 1
    live.wallet = str(Keypair().pubkey())
    with pytest.raises(ProviderError, match="saved for this account"):
        live.reconcile(identifier, token, AT)


def test_proposal_approval_reserves_exact_funded_amount_once(trading_db, monkeypatch):
    now, wallet = fund(monkeypatch)
    p = idea(now)
    assert idea(now, amount=1)["input_raw"] == "250000000"
    assert ledger.rows("trading_orders", proposals.ACCOUNT) == []
    quote = review(p["id"], now)
    assert quote["input_amount"] == "250.000000"
    assert quote["balance_share_percent"] == 25
    assert quote["wallet"] == wallet
    order_id = proposals.approve(p["id"], quote["review_token"], "operator", at=now)
    assert proposals.approve(p["id"], quote["review_token"], "operator", at=now) == order_id
    assert len(ledger.rows("trading_orders", proposals.ACCOUNT)) == 1
    assert ledger.account(proposals.ACCOUNT)["reserved_usdc"] == 250_000_000
    assert proposals.get(p["id"])["status"] == "approved"


def test_review_refresh_expiry_rejection_and_balance_changes(trading_db, monkeypatch):
    now, _ = fund(monkeypatch)
    p = idea(now)
    old = review(p["id"], now)
    current = review(p["id"], now)
    with pytest.raises(ValueError, match="current quote"):
        proposals.approve(p["id"], old["review_token"], "operator", at=now)
    with pytest.raises(ValueError, match="Refresh the quote"):
        proposals.approve(
            p["id"], current["review_token"], "operator", at=now + timedelta(minutes=2)
        )
    with connection() as db:
        db.execute("UPDATE trading_accounts SET cash_usdc=1000000")
    with pytest.raises(ValueError, match="funded USDC balance"):
        proposals.approve(p["id"], current["review_token"], "operator", at=now)
    proposals.reject(p["id"], "operator")
    with pytest.raises(ValueError):
        proposals.approve(p["id"], current["review_token"], "operator", at=now)
    assert ledger.rows("trading_orders", proposals.ACCOUNT) == []
    assert ledger.account(proposals.ACCOUNT)["reserved_usdc"] == 0


@pytest.mark.parametrize("change", ["minimum", "input", "fee", "wallet", "time", "missing"])
def test_signing_requires_current_exact_approved_terms(trading_db, change):
    from tests.test_trading import AT

    live, quote, _, _ = execution()
    identifier, token, order = reserve(live)
    now = AT
    if change == "minimum":
        quote = replace(quote, minimum_raw=quote.minimum_raw - 1)
    elif change == "input":
        quote = replace(quote, input_raw=quote.input_raw + 1)
    elif change == "fee":
        quote = replace(quote, fee_lamports=5_000_001)
    elif change == "wallet":
        live.wallet = str(Keypair().pubkey())
    elif change == "time":
        now += timedelta(minutes=2)
    else:
        with connection() as db:
            db.execute("DELETE FROM trading_proposals")
    with pytest.raises(ProviderError):
        proposals.require_approval(order, quote, live.wallet, Policy(), now)
    assert ledger.rows("trading_orders", identifier)[0]["signed_transaction"] is None


def test_live_prepare_requires_approval_before_transaction_checks(trading_db, monkeypatch):
    from tests.test_trading import AT

    live, quote, _, _ = execution()
    identifier, token, order = reserve(live)
    with connection() as db:
        db.execute("DELETE FROM trading_proposals")
    monkeypatch.setattr(live, "validate", lambda *_: pytest.fail("Signing path reached"))
    with pytest.raises(ProviderError, match="operator-approved"):
        live.prepare(identifier, token, order, quote, AT)


@pytest.mark.parametrize(
    "chat_type,user_id,chat_id,is_bot",
    [
        ("supergroup", 42, -100, False),
        ("private", 99, 99, False),
        ("private", 42, 99, False),
        ("private", 42, 42, True),
    ],
)
def test_live_tools_require_owner_private_chat(
    trading_db, monkeypatch, chat_type, user_id, chat_id, is_bot
):
    monkeypatch.setenv("DASH_TRADING_TELEGRAM_IDS", "42")
    message = SimpleNamespace(
        chat_type=chat_type, user_id=user_id, chat_id=chat_id, sender_is_bot=is_bot
    )
    assert "error" in dash_trading.act(message, "trading_status", {})


def test_dash_can_read_budget_and_propose_once(trading_db, monkeypatch):
    now, _ = fund(monkeypatch)
    monkeypatch.setenv("DASH_TRADING_TELEGRAM_IDS", "42")
    msg = SimpleNamespace(
        chat_type="private", user_id=42, chat_id=42, sender_is_bot=False, update_id=123
    )
    assert dash_trading.act(msg, "trading_status", {})["available_usdc"] == 1_000_000_000
    args = {
        "mint": MINT,
        "side": "buy",
        "amount_usdc": "250",
        "thesis": "View",
        "invalidation": "Risk",
    }
    first = dash_trading.act(msg, "propose_trade", args)
    assert first["state"] == "proposed"
    assert first == dash_trading.act(msg, "propose_trade", args)
    assert len(proposals.listing()) == 1
    assert ledger.rows("trading_orders", proposals.ACCOUNT) == []


def test_proposal_routes_require_origin_owner_and_recent_auth(client, monkeypatch):
    now, _ = fund(monkeypatch)
    p = idea(now)
    url = f"/api/trading/proposals/{p['id']}"
    origin = {"Origin": "http://testserver"}
    assert client.post(url, json={"action": "preview"}, headers=origin).status_code == 401
    client.cookies.set("runner_session", "reader")
    assert client.post(url, json={"action": "preview"}, headers=origin).status_code == 403
    client.cookies.set("runner_session", "operator")
    assert (
        client.post(
            url, json={"action": "preview"}, headers={"Origin": "https://other.test"}
        ).status_code
        == 403
    )
    data = review(p["id"], now)
    result = client.post(
        url, json={"action": "approve", "review_token": data["review_token"]}, headers=origin
    )
    assert result.status_code == 200
    assert result.json()["order_id"]
    assert "no-store" in result.headers["cache-control"]
    with connection() as db:
        db.execute("UPDATE sessions SET authenticated_at='2000-01-01T00:00:00+00:00'")
    assert client.post(url, json={"action": "preview"}, headers=origin).status_code == 403
    assert p["thesis"] not in client.get("/trading").text


def test_wallet_setup_keeps_secret_private_and_preserves_existing_file(tmp_path):
    path = tmp_path / "private" / "dash.json"
    address = create_wallet(path)
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert len(address) >= 32
    with pytest.raises(FileExistsError):
        create_wallet(path)
    assert path.read_bytes() == original
    linked = path.parent / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(FileExistsError):
        create_wallet(linked)
    assert path.read_bytes() == original


def test_sale_needs_its_own_review_and_keeps_full_quantity(trading_db, monkeypatch):
    from runner_trading.engine import Trader

    now, wallet = fund(monkeypatch)
    p = idea(now, amount=25_000_000)
    q = review(p["id"], now)
    buy = proposals.approve(p["id"], q["review_token"], "operator", at=now)
    token = ledger.acquire(proposals.ACCOUNT, now)
    ledger.fill(
        proposals.ACCOUNT,
        token,
        buy,
        input_raw=25_000_000,
        output_raw=100_000_000,
        fee_lamports=100_000,
        gas_usdc=10_000,
        receipt={"test": True},
        at=now,
    )
    ledger.release(proposals.ACCOUNT, token)
    trader = Trader(
        Policy(),
        mode="live",
        live=SimpleNamespace(wallet=wallet),
        provider=FakeJupiter(lambda: now),
        clock=lambda: now,
        source=lambda: [],
    )
    trader.token = ledger.acquire(proposals.ACCOUNT, now)
    # A risk exit records a reviewable proposal before a sale order exists.
    trader.monitor({MINT: {"early": {"state": "avoid"}}})
    ledger.release(proposals.ACCOUNT, trader.token)
    sales = [p for p in proposals.listing() if p["side"] == "sell"]
    assert len(sales) == 1
    assert len(ledger.rows("trading_orders", proposals.ACCOUNT)) == 1
    q = review(sales[0]["id"], now)
    assert q["input_amount"] == "100.000000"
    sell = proposals.approve(sales[0]["id"], q["review_token"], "operator", at=now)
    order = next(o for o in ledger.rows("trading_orders", proposals.ACCOUNT) if o["id"] == sell)
    assert order["input_raw"] == "100000000"
    assert order["side"] == "sell"
    assert order["position_id"] == buy


def test_live_scanner_waits_for_an_approved_proposal(trading_db, monkeypatch):
    from runner_trading.engine import Trader

    now, wallet = fund(monkeypatch)
    adapter = SimpleNamespace(wallet=wallet, reconcile=lambda *_: None)
    trader = Trader(
        Policy(),
        mode="live",
        live=adapter,
        provider=FakeJupiter(lambda: now),
        clock=lambda: now,
        source=lambda: [candidate(now)],
    )
    assert trader.cycle()["state"] == "ok"
    assert ledger.rows("trading_orders", proposals.ACCOUNT) == []
    assert "operator review" in ledger.rows("trading_signals", proposals.ACCOUNT)[0]["reason"]
    p = idea(now, amount=25_000_000)
    q = review(p["id"], now)
    proposals.approve(p["id"], q["review_token"], "operator", at=now)
    assert len(ledger.rows("trading_orders", proposals.ACCOUNT)) == 1
    assert ledger.rows("trading_signals", proposals.ACCOUNT)[0]["state"] == "ordered"


def test_signing_rechecks_approval_after_slow_simulation(trading_db, monkeypatch):
    from tests.test_trading import AT

    live, quote, _, wire = execution()
    identifier, token, order = reserve(live)
    validate = live.validate

    def slow_validate(q):
        transaction = validate(q)
        wire.clock = lambda: AT + timedelta(minutes=2)
        return transaction

    monkeypatch.setattr(live, "validate", slow_validate)
    with pytest.raises(ProviderError):
        live.prepare(identifier, token, order, quote, AT)
    assert ledger.rows("trading_orders", identifier)[0]["signed_transaction"] is None
