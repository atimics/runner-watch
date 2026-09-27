from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from runner_web import db
from runner_web.helius_discovery import _encode
from runner_web.memecoin_chain_parser import PUMP
from runner_web.memecoin_watch import (
    alert_text,
    coin_id_for,
    creator_sells,
    dispatch_risk_alerts,
    launch_bundle,
)
from runner_web.solana_keys import TOKEN_2022_PROGRAM, bonding_curve, token_account

AT = datetime(2026, 9, 27, 12, tzinfo=UTC)
MINT = _encode(bytes([11]) * 32)
CREATOR = _encode(bytes([12]) * 32)
BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])


def signature(number):
    return _encode(number.to_bytes(4, "big") * 16)


def token_balance(amount):
    return {
        "data": {
            "parsed": {"info": {"tokenAmount": {"amount": str(int(amount * 10**6)), "decimals": 6}}}
        }
    }


def row(**extra):
    return {"token_address": MINT, "discovery": {"declared_creator": CREATOR}, **extra}


class FakeRpc:
    def __init__(self, accounts=None, signatures=None, transactions=None):
        self.accounts = accounts or {}
        self.signatures = signatures or {}
        self.transactions = transactions or {}
        self.credits = 0

    def __call__(self, body, *, credits):
        self.credits += credits
        method, params = body["method"], body["params"]
        if method == "getMultipleAccounts":
            return {"result": {"value": [self.accounts.get(a) for a in params[0]]}}
        if method == "getSignaturesForAddress":
            pages = self.signatures.get(params[0], [])
            before = params[1].get("before")
            index = (
                0
                if before is None
                else next(i for i, p in enumerate(pages) if p[-1]["signature"] == before) + 1
            )
            return {"result": pages[index] if index < len(pages) else []}
        if method == "getTransaction":
            return {"result": self.transactions.get(params[0])}
        raise AssertionError(method)


def test_a_creator_balance_drop_is_a_creator_sell():
    account = token_account(CREATOR, MINT, TOKEN_2022_PROGRAM)
    rpc = FakeRpc(
        accounts={account: token_balance(50_000_000)},
        signatures={account: [[{"signature": signature(1), "slot": 9}]]},
    )

    # First look: remember the balance, nothing to compare yet.
    findings, balances = creator_sells([row()], {}, rpc=rpc, at=AT)
    assert findings == [] and balances[MINT]["amount"] == 50_000_000
    rpc.accounts[account] = token_balance(30_000_000)
    findings, balances = creator_sells([row()], balances, rpc=rpc, at=AT + timedelta(minutes=5))

    assert len(findings) == 1
    assert findings[0]["kind"] == "creator_sell"
    assert findings[0]["title"] == "Creator sold or moved 40% of their tokens (2.0% of supply)"
    assert findings[0]["evidence"][0]["signature"] == signature(1)
    assert balances[MINT]["amount"] == 30_000_000


def test_dust_and_new_coins_are_not_creator_sells():
    account = token_account(CREATOR, MINT, TOKEN_2022_PROGRAM)
    rpc = FakeRpc(accounts={account: token_balance(1_000_000)})
    _, balances = creator_sells([row()], {}, rpc=rpc, at=AT)
    rpc.accounts[account] = token_balance(999_990)

    findings, _ = creator_sells([row()], balances, rpc=rpc, at=AT)

    assert findings == []
    # The known account is read alone after the first look: one credit a cycle.
    assert rpc.credits == 2


def curve_buy(number, wallet, tokens, slot):
    curve = bonding_curve(MINT)
    return {
        "slot": slot,
        "blockTime": int(AT.timestamp()) - 3600,
        "meta": {
            "err": None,
            "innerInstructions": [],
            "preTokenBalances": [
                {"mint": MINT, "owner": wallet, "uiTokenAmount": {"amount": "0", "decimals": 6}}
            ],
            "postTokenBalances": [
                {
                    "mint": MINT,
                    "owner": wallet,
                    "uiTokenAmount": {"amount": str(tokens * 10**6), "decimals": 6},
                }
            ],
        },
        "transaction": {
            "signatures": [signature(number)],
            "message": {
                "instructions": [
                    {
                        "programId": PUMP,
                        "accounts": [
                            CREATOR,
                            CREATOR,
                            MINT,
                            curve,
                            CREATOR,
                            CREATOR,
                            wallet,
                            CREATOR,
                        ],
                        "data": _encode(BUY + bytes(24)),
                    }
                ]
            },
        },
    }


def bundled_rpc(buyers, *, tokens_each=50_000_000):
    curve = bonding_curve(MINT)
    transactions = {
        signature(n): curve_buy(n, _encode(bytes([20 + n]) * 32), tokens_each, 500)
        for n in range(buyers)
    }
    later = [{"signature": signature(100 + n), "slot": 600 + n} for n in range(5)]
    launch = [{"signature": signature(n), "slot": 500} for n in reversed(range(buyers))]
    return FakeRpc(signatures={curve: [list(reversed(later)) + launch]}, transactions=transactions)


def test_several_wallets_buying_in_the_launch_slot_are_a_bundle():
    finding = launch_bundle(MINT, rpc=bundled_rpc(4), at=AT)

    assert finding["kind"] == "synchronized_buys"
    assert finding["title"] == "Launch bundle: 4 wallets took 20% of supply in the launch block"
    assert len(finding["related_wallets"]) == 4 and finding["launch_slot"] == 500


def test_a_launch_with_one_buyer_is_clean():
    assert launch_bundle(MINT, rpc=bundled_rpc(1), at=AT) is None


def test_a_launch_thousands_of_trades_back_is_out_of_reach():
    curve = bonding_curve(MINT)
    pages = [
        [{"signature": signature(page * 1000 + n), "slot": 10_000 - page} for n in range(1000)]
        for page in range(5)
    ]

    with pytest.raises(LookupError):
        launch_bundle(MINT, rpc=FakeRpc(signatures={curve: pages}), at=AT)


def test_the_alert_leads_with_the_contract_address():
    finding = {
        "kind": "creator_sell",
        "token_address": MINT,
        "title": "Creator sold or moved 40% of their tokens (2.0% of supply)",
    }

    text = alert_text(finding, origin="https://runners.test")

    assert text.startswith("\U0001f534 *Creator selling*\n`" + MINT + "`")
    assert "40% of their tokens \\(2\\.0% of supply\\)" in text
    assert coin_id_for(MINT) in text


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "watch.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setenv("TELEGRAM_MEMECOIN_ALERTS", "1")
    monkeypatch.setenv("TELEGRAM_API_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-channel")
    db.init_db()


def _store(findings):
    import json

    with db.connection() as database:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
            ("memecoin_watch_findings", json.dumps(findings), AT.isoformat()),
        )


def _finding(state_before, number=1):
    return {
        "id": f"f{number}",
        "kind": "synchronized_buys",
        "token_address": MINT,
        "title": "Launch bundle: 4 wallets took 20% of supply in the launch block",
        "observed_at": AT.isoformat(),
        "state_before": state_before,
    }


def test_a_finding_on_a_watched_coin_is_posted_once(database):
    _store([_finding("setup")])
    posts = []

    first = dispatch_risk_alerts(
        origin="https://runners.test", at=AT, sender=lambda _c, text: posts.append(text) or 1
    )
    again = dispatch_risk_alerts(
        origin="https://runners.test",
        at=AT + timedelta(hours=1),
        sender=lambda _c, text: posts.append(text) or 1,
    )

    assert first["status"] == "sent" and again["status"] == "idle"
    assert len(posts) == 1 and "Launch bundle" in posts[0]


def test_a_finding_on_an_unwatched_coin_is_not_posted(database):
    _store([_finding(None)])

    result = dispatch_risk_alerts(
        origin="https://runners.test", at=AT, sender=lambda *_: pytest.fail("posted")
    )

    assert result["status"] == "idle"
