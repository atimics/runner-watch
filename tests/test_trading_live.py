from __future__ import annotations

import base64
import json
import struct
from datetime import timedelta

import pytest
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from runner_trading import ledger
from runner_trading.config import USDC, Policy, stamp
from runner_trading.live import JUPITER, ROUTE, SYSTEM, Live, load_signer, route_amounts
from runner_trading.providers import ProviderError, Quote
from runner_web.db import connection
from runner_web.solana_keys import TOKEN_PROGRAM, token_account
from tests.test_trading import AT, MINT, candidate
from tests.test_trading import trading_db as trading_db


def encoded(raw, owner=TOKEN_PROGRAM, lamports=2_039_280):
    return {
        "data": [base64.b64encode(bytes(raw)).decode(), "base64"],
        "owner": owner,
        "lamports": lamports,
        "executable": False,
    }


def mint_account():
    raw = bytearray(82)
    raw[45] = 1
    return encoded(raw)


def token_data(mint, wallet, amount):
    raw = bytearray(165)
    raw[:32] = bytes(Pubkey.from_string(mint))
    raw[32:64] = bytes(Pubkey.from_string(wallet))
    raw[64:72] = amount.to_bytes(8, "little")
    raw[108] = 1
    return encoded(raw)


def route_data(amount=25_000_000, output=100_000_000, slippage=100):
    # One Raydium route, 100 percent, input slot 0 -> output slot 1.
    return (
        ROUTE
        + struct.pack("<I", 1)
        + bytes([7, 100, 0, 1])
        + struct.pack("<QQHB", amount, output, slippage, 0)
    )


class Wire:
    def __init__(self):
        self.clock = lambda: AT
        self.sent = []
        self.timeout = True

    def sol_price_usdc(self):
        return 100_000_000

    def request(self, path, body):
        self.sent.append(body)
        if self.timeout:
            raise ProviderError("Lost response")
        return {"status": "Success", "signature": "provider-reply-is-not-settlement"}


class Chain:
    def __init__(self, wallet):
        self.wallet = wallet
        self.source = token_account(wallet, USDC, TOKEN_PROGRAM)
        self.destination = token_account(wallet, MINT, TOKEN_PROGRAM)
        self.data = {
            USDC: mint_account(),
            MINT: mint_account(),
            wallet: encoded(b"", SYSTEM, 50_000_000),
            self.source: token_data(USDC, wallet, 1_000_000_000),
            self.destination: None,
        }
        self.status = None
        self.receipt = None
        self.height = 100
        self.sim_error = None
        self.delegate = False

    def __call__(self, method, params):
        if method == "getMultipleAccounts":
            return {"value": [self.data.get(key) for key in params[0]]}
        if method == "simulateTransaction":
            output = token_data(MINT, self.wallet, 100_000_000)
            if self.delegate:
                raw = bytearray(base64.b64decode(output["data"][0]))
                raw[72] = 1
                output = encoded(raw)
            return {
                "value": {
                    "err": self.sim_error,
                    "accounts": [
                        encoded(b"", SYSTEM, 47_900_000),
                        token_data(USDC, self.wallet, 975_000_000),
                        output,
                    ],
                }
            }
        if method == "getBlockHeight":
            return self.height
        if method == "getSignatureStatuses":
            return {"context": {"slot": 500}, "value": [self.status]}
        if method == "getSlot":
            return 500
        if method == "getTransaction":
            return self.receipt
        if method == "getTokenAccountsByOwner":
            return {
                "value": [
                    {
                        "account": {
                            "data": {
                                "parsed": {
                                    "info": {"mint": USDC, "tokenAmount": {"amount": "1000000000"}}
                                }
                            }
                        }
                    }
                ]
            }
        if method == "getBalance":
            return {"value": 50_000_000}
        pytest.fail(f"Unexpected RPC {method}")


def execution():
    signer = Keypair()
    wallet = str(signer.pubkey())
    source, target = (token_account(wallet, mint, TOKEN_PROGRAM) for mint in (USDC, MINT))
    accounts = [
        AccountMeta(Pubkey.from_string(TOKEN_PROGRAM), False, False),
        AccountMeta(signer.pubkey(), True, False),
        AccountMeta(Pubkey.from_string(source), False, True),
        AccountMeta(Pubkey.from_string(target), False, True),
        AccountMeta(Pubkey.from_string(JUPITER), False, False),
        AccountMeta(Pubkey.from_string(MINT), False, False),
        AccountMeta(Pubkey.from_string(JUPITER), False, False),
    ]
    ix = Instruction(Pubkey.from_string(JUPITER), route_data(), accounts)
    message = MessageV0.try_compile(signer.pubkey(), [ix], [], Hash.new_unique())
    unsigned = VersionedTransaction.populate(message, [Signature.default()])
    quote = Quote(
        USDC,
        MINT,
        25_000_000,
        100_000_000,
        99_000_000,
        2_100_000,
        0,
        stamp(AT),
        {
            "transaction": base64.b64encode(bytes(unsigned)).decode(),
            "lastValidBlockHeight": "250",
            "requestId": "fixed-request",
            "router": "metis",
        },
    )
    wire, chain = Wire(), Chain(wallet)
    return Live(Policy(), wire, signer=signer, rpc=chain), quote, chain, wire


def reserve(live):
    identifier = ledger.ensure_account("live", Policy(), AT)["id"]
    token = ledger.acquire(identifier, AT)
    with connection() as db:
        db.execute(
            "INSERT INTO trading_signals(id,account_id,mint,coin_id,observed_at,state,reason,"
            "evidence_json,outcome_due_at) VALUES(?,?,?,?,?,'observed','checked',?,?)",
            (
                "signal",
                identifier,
                MINT,
                "test",
                stamp(AT),
                json.dumps(candidate()),
                stamp(AT + timedelta(hours=6)),
            ),
        )
    ledger.reserve_buy(identifier, token, {"id": "signal", "mint": MINT}, Policy(), AT)
    return identifier, token, ledger.rows("trading_orders", identifier)[0]


def finalized(order, live, *, error=None, output=100_000_000):
    def balance(mint, amount):
        return {"mint": mint, "owner": live.wallet, "uiTokenAmount": {"amount": str(amount)}}

    return {
        "slot": 123,
        "transaction": {
            "signatures": [order["signature"]],
            "message": {"accountKeys": [live.wallet]},
        },
        "meta": {
            "err": error,
            "preBalances": [50_000_000],
            "postBalances": [47_900_000],
            "preTokenBalances": [balance(USDC, 1_000_000_000)],
            "postTokenBalances": [balance(USDC, 975_000_000), balance(MINT, output)],
        },
    }


def test_live_signs_only_after_simulation_and_saves_before_send(trading_db):
    live, quote, chain, wire = execution()
    identifier, token, order = reserve(live)
    live.prepare(identifier, token, order, quote, AT)
    saved = ledger.rows("trading_orders", identifier)[0]
    assert saved["status"] == "prepared" and saved["signature"]
    assert wire.sent == []
    signed = VersionedTransaction.from_bytes(base64.b64decode(saved["signed_transaction"]))
    assert signed.verify_with_results() == [True]
    live.recover(identifier, token, saved, AT)
    live.recover(identifier, token, ledger.rows("trading_orders", identifier)[0], AT)
    assert wire.sent[0] == wire.sent[1]
    assert ledger.account(identifier)["reserved_usdc"] == 25_000_000
    chain.receipt = finalized(saved, live)
    live.recover(identifier, token, saved, AT)
    live.recover(identifier, token, saved, AT)
    assert ledger.account(identifier)["cash_usdc"] == 975_000_000
    assert ledger.account(identifier)["reserved_usdc"] == 0
    assert len(ledger.rows("trading_positions", identifier)) == 1


@pytest.mark.parametrize("failed", [True, False])
def test_finalized_failure_or_expiry_releases_the_reservation_once(trading_db, failed):
    live, quote, chain, wire = execution()
    identifier, token, order = reserve(live)
    live.prepare(identifier, token, order, quote, AT)
    saved = ledger.rows("trading_orders", identifier)[0]
    if failed:
        chain.receipt = finalized(saved, live, error={"InstructionError": [0, "failure"]})
    else:
        chain.height = 251
    live.recover(identifier, token, saved, AT)
    live.recover(identifier, token, saved, AT)
    acc = ledger.account(identifier)
    assert acc["reserved_usdc"] == 0 and acc["cash_usdc"] == 1_000_000_000
    assert acc["sol_lamports"] == (47_900_000 if failed else 50_000_000)


def test_seen_signature_past_expiry_still_waits_for_finality(trading_db):
    live, quote, chain, wire = execution()
    identifier, token, order = reserve(live)
    live.prepare(identifier, token, order, quote, AT)
    saved = ledger.rows("trading_orders", identifier)[0]
    chain.height = 251
    chain.status = {"confirmationStatus": "confirmed"}
    live.recover(identifier, token, saved, AT)
    assert ledger.account(identifier)["reserved_usdc"] == 25_000_000
    assert wire.sent == []


def test_a_lagging_status_read_keeps_the_expired_order_reserved(trading_db):
    live, quote, chain, wire = execution()
    identifier, token, order = reserve(live)
    live.prepare(identifier, token, order, quote, AT)
    saved = ledger.rows("trading_orders", identifier)[0]
    chain.height = 251

    def lagging(method, params):
        return (
            {"context": {"slot": 499}, "value": [None]}
            if method == "getSignatureStatuses"
            else chain(method, params)
        )

    live.rpc = lagging
    with pytest.raises(ProviderError, match="consistent"):
        live.recover(identifier, token, saved, AT)
    assert ledger.account(identifier)["reserved_usdc"] == 25_000_000


def test_unexpected_finalized_output_pauses_and_preserves_evidence(trading_db):
    live, quote, chain, wire = execution()
    identifier, token, order = reserve(live)
    live.prepare(identifier, token, order, quote, AT)
    saved = ledger.rows("trading_orders", identifier)[0]
    chain.receipt = finalized(saved, live, output=1)
    live.recover(identifier, token, saved, AT)
    assert ledger.account(identifier)["paused"]
    assert ledger.account(identifier)["reserved_usdc"] == 25_000_000
    assert len(ledger.rows("trading_positions", identifier)) == 0
    evidence = json.loads(ledger.rows("trading_orders", identifier)[0]["result_json"])
    assert evidence["review_required"] is True
    assert evidence["signature"] == saved["signature"]
    assert evidence["credit_raw"] == "1"


@pytest.mark.parametrize("change", ["simulation", "delegate", "controls", "amount", "program"])
def test_unsafe_transactions_are_rejected_before_signing(change):
    live, quote, chain, wire = execution()
    if change == "simulation":
        chain.sim_error = {"failure": True}
    elif change == "delegate":
        chain.delegate = True
    elif change == "controls":
        raw = bytearray(82)
        raw[0] = raw[45] = 1
        chain.data[MINT] = encoded(raw)
    else:
        tx = VersionedTransaction.from_bytes(base64.b64decode(quote.raw["transaction"]))
        ix = tx.message.instructions[0]
        accounts = [AccountMeta(tx.message.account_keys[i], i == 0, True) for i in ix.accounts]
        changed = Instruction(
            Pubkey.from_string(SYSTEM if change == "program" else JUPITER),
            route_data(amount=1),
            accounts,
        )
        message = MessageV0.try_compile(live.signer.pubkey(), [changed], [], Hash.new_unique())
        quote.raw["transaction"] = base64.b64encode(
            bytes(VersionedTransaction.populate(message, [Signature.default()]))
        ).decode()
    with pytest.raises(ProviderError):
        live.validate(quote)
    assert wire.sent == []


def test_route_parser_checks_exact_length_and_known_variants():
    assert route_amounts(route_data()) == (25_000_000, 100_000_000, 100, 0)
    for data in (
        route_data() + b"forged-trailer",
        route_data()[:12] + b"\xff" + route_data()[13:],
        b"x",
    ):
        with pytest.raises(ProviderError):
            route_amounts(data)


def test_signer_requires_explicit_live_mode_owned_file_and_matching_wallet(tmp_path, monkeypatch):
    signer = Keypair()
    path = tmp_path / "signer.json"
    path.write_text(json.dumps(list(bytes(signer))))
    path.chmod(0o600)
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("TRADING_LIVE_ENABLED", "1")
    monkeypatch.setenv("TRADING_WALLET", str(signer.pubkey()))
    monkeypatch.setenv("TRADING_KEY_FILE", str(path))
    with pytest.raises(ValueError, match="dedicated"):
        load_signer()
    monkeypatch.setenv("TRADING_MODE", "live")
    assert load_signer().pubkey() == signer.pubkey()
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_signer()
