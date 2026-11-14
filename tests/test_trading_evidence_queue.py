import copy
import json
from datetime import timedelta

import pytest

from runner_trading import ledger
from runner_trading.config import Policy, stamp
from runner_trading.engine import Trader
from tests.test_trading import AT, MINT, FakeJupiter, candidate, setup_trader
from tests.test_trading import trading_db as trading_db


def deferred(rows, **kwargs):
    return [{**row, "_trading_evidence_deferred": True} for row in rows]


def worker(clock, board, enricher, cache=None):
    return Trader(
        Policy(),
        clock=lambda: clock["at"],
        source=lambda: board,
        provider=FakeJupiter(lambda: clock["at"]),
        evidence_enricher=enricher,
        evidence_cache=cache,
    )


def test_deferred_signal_survives_refresh_restart_and_saves_actual_decision_evidence(trading_db):
    clock, board = {"at": AT}, [candidate()]
    trader = worker(clock, board, deferred)
    trader.cycle()
    saved = ledger.rows("trading_signals", trader.identifier)[0]
    assert saved["state"] == "observed"
    assert ledger.rows("trading_orders", trader.identifier) == []

    clock["at"] += timedelta(seconds=60)
    board[:] = [candidate(at=clock["at"])]

    def sampled(rows, **kwargs):
        return [{**row, "sample_receipt": "targeted-pool-read"} for row in rows]

    restarted = worker(clock, board, sampled)
    restarted.cycle()
    signals = ledger.rows("trading_signals", trader.identifier)
    assert len(signals) == 1 and signals[0]["id"] == saved["id"]
    assert signals[0]["observed_at"] == stamp(AT)
    assert signals[0]["state"] == "ordered"
    evidence = json.loads(signals[0]["evidence_json"])
    assert evidence["observed_at"] == stamp(clock["at"])
    assert evidence["sample_receipt"] == "targeted-pool-read"
    assert "_trading_evidence_deferred" not in evidence
    clock["at"] += timedelta(seconds=30)
    restarted.cycle()
    assert len(ledger.rows("trading_positions", trader.identifier, active=True)) == 1
    assert len(ledger.rows("trading_signals", trader.identifier)) == 1


@pytest.mark.parametrize("change", ["avoid", "risk", "missing"])
def test_pending_signal_rechecks_current_risk_before_sampling_or_ordering(trading_db, change):
    clock, board = {"at": AT}, [candidate()]
    trader = worker(clock, board, deferred)
    trader.cycle()
    clock["at"] += timedelta(seconds=30)
    board[:] = [candidate(at=clock["at"])]
    if change == "avoid":
        board[0]["early"]["state"] = "avoid"
    elif change == "risk":
        board[0]["memecoin_assessment"]["risk"]["factors"] = ["new risk"]
    else:
        board.clear()

    def should_not_sample(*args, **kwargs):
        pytest.fail("Ineligible pending signal spent a targeted read")

    trader.evidence_enricher = should_not_sample
    trader.cycle()
    signal = ledger.rows("trading_signals", trader.identifier)[0]
    assert signal["state"] == "skipped"
    assert ledger.rows("trading_orders", trader.identifier) == []
    assert json.loads(signal["evidence_json"]) == (board[0] if board else {})


def test_refresh_cannot_extend_deferred_opportunity_deadline(trading_db):
    clock, board = {"at": AT}, [candidate()]
    trader = worker(clock, board, deferred)
    trader.cycle()
    clock["at"] += timedelta(seconds=181)
    board[:] = [candidate(at=clock["at"])]
    restarted = worker(clock, board, lambda *_args, **_kwargs: pytest.fail("Expired sample"))
    restarted.cycle()
    signal = ledger.rows("trading_signals", trader.identifier)[0]
    assert signal["observed_at"] == stamp(AT) and signal["state"] == "skipped"
    assert "Entry quote window ended" in signal["reason"]
    assert ledger.rows("trading_orders", trader.identifier) == []


def test_queued_order_keeps_original_deadline_after_delayed_evidence(trading_db):
    clock, board = {"at": AT}, [candidate()]
    trader = worker(clock, board, deferred)
    trader.cycle()
    clock["at"] += timedelta(seconds=179)
    board[:] = [candidate(at=clock["at"])]
    trader.evidence_enricher = lambda rows, **kwargs: rows
    trader.cycle()
    assert ledger.rows("trading_orders", trader.identifier)[0]["status"] == "queued"
    clock["at"] += timedelta(seconds=4)
    trader.cycle()
    order = ledger.rows("trading_orders", trader.identifier)[0]
    assert order["status"] == "failed" and "Entry time window ended" in order["reason"]
    assert ledger.account(trader.identifier)["reserved_usdc"] == 0
    assert ledger.rows("trading_positions", trader.identifier) == []


def test_exit_execution_precedes_targeted_candidate_read(trading_db):
    trader, clock, board, _provider = setup_trader()
    trader.cycle()
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    clock["at"] += timedelta(hours=7)
    board[:] = [candidate(at=clock["at"], token_address="new-mint", id="new")]

    def enrich_after_exit(rows, **kwargs):
        assert ledger.rows("trading_positions", trader.identifier, active=True) == []
        assert [row["token_address"] for row in rows] == ["new-mint"]
        return rows

    trader.evidence_enricher = enrich_after_exit
    trader.cycle()
    assert len(ledger.rows("trading_orders", trader.identifier)) == 3


def test_queued_buy_uses_cached_targeted_evidence_without_another_paid_read(trading_db):
    clock, board = {"at": AT}, [candidate()]
    board[0]["memecoin_assessment"]["coverage"] = {"chain_checked_at": stamp(AT)}
    cache = {}
    reads = []

    def sampled(rows, **kwargs):
        reads.append(len(rows))
        result = copy.deepcopy(rows)
        for row in result:
            cache[row["token_address"]] = candidate()["memecoin_assessment"]["coverage"]
            row["memecoin_assessment"]["coverage"].update(cache[row["token_address"]])
        return result

    def cached(row, **kwargs):
        result = copy.deepcopy(row)
        result["memecoin_assessment"]["coverage"].update(cache.get(row["token_address"], {}))
        return result

    trader = worker(clock, board, sampled, cached)
    trader.cycle()
    clock["at"] += timedelta(seconds=30)
    trader.cycle()
    assert reads == [1]
    assert [p["mint"] for p in ledger.rows("trading_positions", trader.identifier)] == [MINT]


def test_deferred_rows_do_not_consume_candidate_quote_budget(trading_db):
    clock = {"at": AT}
    board = [candidate(token_address=f"mint{i}", id=f"coin{i}") for i in range(11)]

    def last_candidate_sampled(rows, **kwargs):
        return [
            row if row["token_address"] == "mint10" else {**row, "_trading_evidence_deferred": True}
            for row in rows
        ]

    trader = worker(clock, board, last_candidate_sampled)
    trader.cycle()
    assert [o["mint"] for o in ledger.rows("trading_orders", trader.identifier)] == ["mint10"]
    assert (
        sum(s["state"] == "observed" for s in ledger.rows("trading_signals", trader.identifier))
        == 10
    )
