from datetime import timedelta

import pytest

from runner_trading import ledger
from runner_trading.config import USDC, Policy
from runner_trading.engine import Trader
from runner_trading.providers import Jupiter, ProviderError, QuoteRejected
from tests.test_trading import AT, FakeJupiter, candidate, setup_trader
from tests.test_trading import trading_db as trading_db


@pytest.mark.parametrize("failure", [QuoteRejected, ProviderError])
def test_candidate_rejection_is_local_but_provider_failure_closes_entry_gate(trading_db, failure):
    clock = {"at": AT}

    class Provider(FakeJupiter):
        def quote(self, input_mint, output_mint, amount, **kwargs):
            if output_mint == "rejected":
                raise failure("Swap price impact exceeds the limit")
            return super().quote(input_mint, output_mint, amount, **kwargs)

    board = [
        candidate(token_address="rejected", id="rejected"),
        candidate(token_address="eligible", id="eligible"),
    ]
    provider = Provider(lambda: clock["at"])
    trader = Trader(Policy(), clock=lambda: clock["at"], provider=provider, source=lambda: board)
    health = trader.cycle()
    signals = {s["mint"]: s for s in ledger.rows("trading_signals", trader.identifier)}
    assert signals["rejected"]["state"] == "skipped"
    if failure is QuoteRejected:
        assert health["state"] == "ok" and health["errors"] == []
        assert signals["eligible"]["state"] == "ordered"
        clock["at"] += timedelta(seconds=30)
        trader.cycle()
        assert [p["mint"] for p in ledger.rows("trading_positions", trader.identifier)] == [
            "eligible"
        ]
    else:
        assert health["state"] == "degraded"
        assert "Restore provider health" in signals["eligible"]["reason"]
        assert ledger.rows("trading_orders", trader.identifier) == []


def test_rejected_exit_stays_unavailable_and_retries_without_releasing_position(trading_db):
    trader, clock, board, provider = setup_trader()
    trader.cycle()
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    original_quote = provider.quote

    def rejected_exit(input_mint, output_mint, amount, **kwargs):
        if output_mint == USDC:
            raise QuoteRejected("Network and account costs exceed the limit")
        return original_quote(input_mint, output_mint, amount, **kwargs)

    provider.quote = rejected_exit
    board.clear()
    clock["at"] += timedelta(hours=7)
    health = trader.cycle()
    assert health["state"] == "degraded" and health["unavailable_exits"] == 1
    position = ledger.rows("trading_positions", trader.identifier, active=True)[0]
    assert position["quantity_raw"] == "100000000"
    order = ledger.rows("trading_orders", trader.identifier, active=True)[0]
    assert order["side"] == "sell" and order["attempts"] == 1
    provider.quote = original_quote
    clock["at"] += timedelta(seconds=61)
    trader.cycle()
    assert ledger.rows("trading_positions", trader.identifier, active=True) == []
    assert len(ledger.rows("trading_orders", trader.identifier)) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"priceImpact": 2},
        {"otherAmountThreshold": "98"},
        {"router": "dflow"},
        {"prioritizationFeeLamports": 5_000_001},
    ],
)
def test_quote_policy_failures_are_distinct_from_service_failures(change):
    raw = {
        "inputMint": USDC,
        "outputMint": "mint",
        "inAmount": "25000000",
        "outAmount": "101",
        "otherAmountThreshold": "99",
        "swapMode": "ExactIn",
        "router": "metis",
        "priceImpact": 0.1,
        **change,
    }
    provider = Jupiter(Policy(), transport=lambda *_: raw, clock=lambda: AT, pace=False)
    with pytest.raises(QuoteRejected):
        provider.quote(USDC, "mint", 25_000_000)


@pytest.mark.parametrize("change", [{"inAmount": "1"}, {"otherAmountThreshold": "102"}])
def test_inconsistent_provider_response_still_reports_provider_failure(change):
    raw = {
        "inputMint": USDC,
        "outputMint": "mint",
        "inAmount": "25000000",
        "outAmount": "101",
        "otherAmountThreshold": "99",
        "swapMode": "ExactIn",
        "router": "metis",
        "priceImpact": 0.1,
        **change,
    }
    provider = Jupiter(Policy(), transport=lambda *_: raw, clock=lambda: AT, pace=False)
    with pytest.raises(ProviderError) as error:
        provider.quote(USDC, "mint", 25_000_000)
    assert not isinstance(error.value, QuoteRejected)
