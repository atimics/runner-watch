from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import pytest

from runner_trading import ledger
from runner_trading.config import SOL, USDC, Policy, stamp
from runner_trading.engine import Trader
from runner_trading.providers import Jupiter, ProviderError, Quote
from runner_trading.strategy import entry_reasons
from runner_web import db

AT = datetime(2026, 10, 25, 17, tzinfo=UTC)
MINT = "ARPwPPWbaj3FYBf6k1Lt2jqRkv9JJUg3Hxav5aHKTEem"


@pytest.fixture
def trading_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "trading.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    admin_url = os.getenv("TRADING_TEST_DATABASE_URL")
    if not admin_url:
        db.init_db()
        yield
        return
    import psycopg
    from psycopg import sql

    from runner_web.database import close_database_pool

    parts = urlsplit(admin_url)
    if parts.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("The PostgreSQL trading test requires a local test service")
    name = "trading_test_" + uuid.uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        try:
            monkeypatch.setattr(db, "DATABASE_URL", urlunsplit(parts._replace(path="/" + name)))
            monkeypatch.setattr(db, "REQUIRE_DATABASE_TLS", False)
            db.init_db()
            yield
        finally:
            close_database_pool()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def candidate(at=AT, **changes):
    return {
        "id": "solana:example",
        "token_address": MINT,
        "network": "solana",
        "venue": "pool",
        "observed_at": stamp(at),
        "early": {"state": "setup"},
        "real_liquidity_usd": 100_000,
        "ratification": {"ratified": True, "as_of": stamp(at)},
        "trading_checks": {
            "holder_rules": 7,
            "holder_checked_at": stamp(at),
            "lock_checked_at": stamp(at),
            "controls_clean": True,
        },
        "memecoin_assessment": {
            "coverage": {"chain_checked_at": stamp(at)},
            "risk": {"factors": []},
            "sentiment": {"observed_swaps": 4},
        },
        **changes,
    }


class FakeJupiter:
    def __init__(self, clock):
        self.clock = clock
        self.output = 25_000_000
        self.fail = False
        self.calls = []

    def quote(self, input_mint, output_mint, amount, **kwargs):
        self.calls.append((input_mint, output_mint, amount))
        if self.fail:
            raise ProviderError("Exit route unavailable")
        return Quote(
            input_mint,
            output_mint,
            amount,
            100_000_000 if input_mint == USDC else self.output,
            100_000_000 if input_mint == USDC else self.output,
            100_000,
            0,
            stamp(self.clock()),
            {"router": "metis"},
        )

    def gas_cost(self, amount):
        return amount // 10

    def paper_output(self, quote):
        return quote.output_raw


def setup_trader():
    clock = {"at": AT}
    source = [candidate()]
    provider = FakeJupiter(lambda: clock["at"])
    trader = Trader(Policy(), clock=lambda: clock["at"], provider=provider, source=lambda: source)
    return trader, clock, source, provider


def test_paper_buy_restart_exit_and_accounting_are_exactly_once(trading_db):
    trader, clock, source, provider = setup_trader()
    trader.cycle()
    account = ledger.account(trader.identifier)
    assert account["cash_usdc"] == 1_000_000_000
    assert account["reserved_usdc"] == 25_000_000
    trader.cycle()
    assert len(ledger.rows("trading_orders", trader.identifier)) == 1
    clock["at"] += timedelta(seconds=30)
    trader = Trader(Policy(), clock=lambda: clock["at"], provider=provider, source=lambda: source)
    trader.cycle()
    positions = ledger.rows("trading_positions", trader.identifier, active=True)
    assert len(positions) == 1 and positions[0]["quantity_raw"] == "100000000"
    assert ledger.account(trader.identifier)["cash_usdc"] == 975_000_000
    assert ledger.account(trader.identifier)["reserved_usdc"] == 0
    source.clear()  # Holding continues after the token leaves the public board.
    provider.output = 35_000_000
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    account = ledger.account(trader.identifier)
    assert account["cash_usdc"] == 1_010_000_000
    assert account["realized_usdc"] == 9_980_000
    assert account["gas_usdc"] == 20_000
    assert account["sol_lamports"] == 49_800_000
    trader.cycle()
    assert len(ledger.rows("trading_orders", trader.identifier)) == 2
    assert ledger.account(trader.identifier)["cash_usdc"] == account["cash_usdc"]


def test_failed_exit_keeps_quantity_and_retries(trading_db):
    trader, clock, source, provider = setup_trader()
    trader.cycle()
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    clock["at"] += timedelta(hours=7)
    source.clear()
    provider.fail = True
    trader.cycle()
    positions = ledger.rows("trading_positions", trader.identifier, active=True)
    assert positions[0]["mark_state"] == "exit_unavailable"
    assert positions[0]["quantity_raw"] == "100000000"
    assert ledger.rows("trading_orders", trader.identifier, active=True)[0]["side"] == "sell"
    provider.fail = False
    clock["at"] += timedelta(seconds=61)
    trader.cycle()
    assert ledger.rows("trading_positions", trader.identifier, active=True) == []


def test_pause_refunds_pending_entry_and_preserves_outcome_cohort(trading_db):
    trader, clock, source, provider = setup_trader()
    trader.cycle()
    ledger.pause(trader.identifier, "Operator pause", clock["at"])
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    assert ledger.account(trader.identifier)["reserved_usdc"] == 0
    source.clear()
    clock["at"] += timedelta(hours=7)
    provider.fail = True
    trader.cycle()
    signal = ledger.rows("trading_signals", trader.identifier)[0]
    assert json.loads(signal["outcome_json"])["state"] == "unavailable"
    assert signal["baseline_units"] == "100000000"


def test_leases_and_stale_worker_fence(trading_db):
    trader, clock, *_ = setup_trader()
    first = ledger.acquire(trader.identifier, AT)
    assert ledger.acquire(trader.identifier, AT) is None
    second = ledger.acquire(trader.identifier, AT + timedelta(minutes=4))
    assert second
    with pytest.raises(RuntimeError, match="lease"):
        with ledger.locked(trader.identifier, first, AT + timedelta(minutes=4)):
            pytest.fail("An expired worker acquired the account")
    ledger.release(trader.identifier, first)
    assert ledger.acquire(trader.identifier, AT + timedelta(minutes=4)) is None


def test_saved_policy_is_immutable(trading_db):
    ledger.ensure_account("paper", Policy(), AT)
    with pytest.raises(ValueError, match="saved account policy"):
        ledger.ensure_account("paper", Policy(entry_usdc=50_000_000), AT)


def test_four_reservations_share_one_cash_and_exposure_limit(trading_db):
    trader, clock, source, provider = setup_trader()
    source[:] = [candidate(token_address=f"mint{i}", id=f"coin{i}") for i in range(6)]
    trader.cycle()
    assert ledger.account(trader.identifier)["reserved_usdc"] == 100_000_000
    assert len(ledger.rows("trading_orders", trader.identifier)) == 4
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    assert len(ledger.rows("trading_positions", trader.identifier, active=True)) == 4
    assert ledger.account(trader.identifier)["cash_usdc"] == 900_000_000
    assert ledger.account(trader.identifier)["reserved_usdc"] == 0


def test_daily_loss_pauses_entries_while_stop_exit_still_fills(trading_db):
    clock = {"at": AT}
    source = [candidate()]
    provider = FakeJupiter(lambda: clock["at"])
    trader = Trader(
        Policy(daily_loss_usdc=1_000_000),
        clock=lambda: clock["at"],
        provider=provider,
        source=lambda: source,
    )
    trader.cycle()
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    provider.output = 10_000_000
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    assert ledger.account(trader.identifier)["paused"] == 1
    assert ledger.rows("trading_positions", trader.identifier, active=True) == []
    assert ledger.account(trader.identifier)["realized_usdc"] == -15_020_000


def test_live_submission_error_keeps_the_saved_signature_and_reservation(trading_db):
    class Adapter:
        wallet = "dedicated-wallet"
        submissions = 0

        def reconcile(self, *args):
            pass

        def prepare(self, identifier, token, order, quote, at):
            with ledger.locked(identifier, token, at) as database:
                database.execute(
                    "UPDATE trading_orders SET status='prepared',signature='saved' WHERE id=?",
                    (order["id"],),
                )

        def recover(self, identifier, token, order, at):
            assert order["signature"] == "saved"
            self.submissions += 1
            raise ProviderError("Confirmation service unavailable")

    clock = {"at": AT}
    adapter = Adapter()
    trader = Trader(
        Policy(),
        mode="live",
        clock=lambda: clock["at"],
        provider=FakeJupiter(lambda: clock["at"]),
        live=adapter,
        source=lambda: [candidate()],
    )
    trader.cycle()
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    assert adapter.submissions == 1  # Same cycle as preparation.
    assert ledger.account(trader.identifier)["reserved_usdc"] == 25_000_000
    assert ledger.rows("trading_orders", trader.identifier)[0]["status"] == "prepared"
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    assert adapter.submissions == 2
    assert ledger.account(trader.identifier)["reserved_usdc"] == 25_000_000


def test_restart_after_signal_capture_records_a_skipped_stale_entry(trading_db):
    trader, clock, source, provider = setup_trader()
    token = ledger.acquire(trader.identifier, AT)
    trader.token = token
    # Simulate a stop after evidence commit, before the first quote.
    with db.connection() as database:
        database.execute(
            "INSERT INTO trading_signals(id,account_id,mint,coin_id,observed_at,"
            "state,reason,evidence_json,outcome_due_at) VALUES(?,?,?,?,?,'observed',?,?,?)",
            (
                "waiting",
                trader.identifier,
                MINT,
                "example",
                stamp(AT),
                "checked",
                json.dumps(candidate()),
                stamp(AT + timedelta(hours=6)),
            ),
        )
    ledger.release(trader.identifier, token)
    source.clear()
    clock["at"] += timedelta(hours=7)
    trader.cycle()
    signal = ledger.rows("trading_signals", trader.identifier)[0]
    assert signal["state"] == "skipped"
    assert signal["baseline_units"] is None
    assert json.loads(signal["outcome_json"])["state"] == "unavailable"
    assert provider.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_at": stamp(AT - timedelta(minutes=6))},
        {"observed_at": stamp(AT + timedelta(seconds=1))},
        {"trading_checks": {}},
        {"ratification": {"ratified": False}},
        {"real_liquidity_usd": float("nan")},
        {"network": "ethereum"},
        {"memecoin_assessment": {}},
        {"venue": "bonding_curve"},
    ],
)
def test_stale_unknown_and_risky_entries_are_recorded_as_skips(changes):
    assert entry_reasons(candidate(**changes), Policy(), AT)
    assert entry_reasons(candidate(), Policy(), AT) == []


def test_provider_checks_identity_amount_impact_and_exit_policy():
    raw = {
        "inputMint": USDC,
        "outputMint": MINT,
        "inAmount": "25000000",
        "outAmount": "101",
        "otherAmountThreshold": "99",
        "swapMode": "ExactIn",
        "router": "metis",
        "priceImpact": 0.1,
    }
    provider = Jupiter(Policy(), transport=lambda *_: raw, clock=lambda: AT, pace=False)
    assert provider.quote(USDC, MINT, 25_000_000).minimum_raw == 99
    raw["priceImpact"] = 2
    with pytest.raises(ProviderError, match="impact"):
        provider.quote(USDC, MINT, 25_000_000)
    raw.update(inputMint=MINT, outputMint=USDC)
    assert provider.quote(MINT, USDC, 25_000_000).impact_bps == 200
    with pytest.raises(ProviderError, match="differs"):
        provider.quote(SOL, USDC, 25_000_000)
