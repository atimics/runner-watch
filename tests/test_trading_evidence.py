from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from runner_trading import evidence
from runner_trading.config import Policy, stamp
from runner_trading.strategy import entry_reasons, non_swap_entry_reasons
from runner_web import db
from runner_web import helius_discovery as helius
from runner_web import memecoin_evidence as store
from runner_web.memecoin_chain_parser import BUY

AT = datetime(2026, 11, 14, 23, tzinfo=UTC)
MINT = helius._encode(bytes([2]) * 32)
POOL = helius._encode(bytes([3]) * 32)
WALLET = helius._encode(bytes([4]) * 32)


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "evidence.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setenv("HELIUS_DAILY_CREDITS", "10000")
    db.init_db()


def candidate(mint=MINT, pool=POOL):
    return {
        "id": "solana:" + mint,
        "network": "solana",
        "venue": "pool",
        "token_address": mint,
        "pool_address": pool,
        "observed_at": stamp(AT),
        "early": {"state": "setup"},
        "real_liquidity_usd": 100_000,
        "ratification": {"ratified": True, "as_of": stamp(AT)},
        "trading_checks": {
            "holder_rules": 7,
            "holder_checked_at": stamp(AT),
            "lock_checked_at": stamp(AT),
            "controls_clean": True,
        },
        "memecoin_assessment": {
            "coverage": {"chain_checked_at": stamp(AT)},
            "risk": {"factors": []},
            "sentiment": {"observed_swaps": 0},
        },
    }


def transaction(index, *, mint=MINT, pool=POOL, seconds=None):
    def balance(amount):
        return {
            "mint": mint,
            "owner": WALLET,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6},
        }

    return {
        "slot": 100 + index,
        "blockTime": int(AT.timestamp()) - (index * 40 if seconds is None else seconds),
        "meta": {
            "err": None,
            "innerInstructions": [],
            "preTokenBalances": [balance(0)],
            "postTokenBalances": [balance(1_000_000)],
        },
        "transaction": {
            "signatures": [helius._encode(bytes([index]) * 64)],
            "message": {
                "instructions": [
                    {
                        "programId": helius.PUMP_SWAP,
                        "accounts": [pool, WALLET, pool, mint],
                        "data": helius._encode(BUY + bytes(24)),
                    }
                ]
            },
        },
    }


def page(entries):
    return {"result": {"data": entries}}


def test_real_finalized_parser_to_entry_and_cached_revalidation():
    calls = []

    def rpc(request):
        calls.append(request)
        return page([transaction(i) for i in (1, 2, 3)])

    original = candidate()
    result = evidence.enrich_candidates([original], at=AT, policy=Policy(), rpc=rpc)[0]
    assert entry_reasons(result, Policy(), AT) == []
    assert original["memecoin_assessment"]["sentiment"]["observed_swaps"] == 0
    options = calls[0]["params"][1]
    assert calls[0]["params"][0] == POOL
    assert options["commitment"] == "finalized" and options["sortOrder"] == "desc"
    assert options["limit"] == 100
    coverage = result["memecoin_assessment"]["coverage"]
    assert coverage["swap_checked_at"] == stamp(AT)
    assert coverage["chain_checked_at"] == stamp(AT)
    cached = evidence.apply_cached_evidence(candidate(), at=AT + timedelta(seconds=30))
    assert entry_reasons(cached, Policy(), AT + timedelta(seconds=30)) == []
    evidence.enrich_candidates(
        [candidate()], at=AT + timedelta(seconds=30), policy=Policy(), rpc=rpc
    )
    assert len(calls) == 1
    with db.connection() as database:
        assert (
            database.execute("SELECT COUNT(*) FROM memecoin_chain_transactions").fetchone()[0] == 3
        )


def test_wrong_mint_pool_old_future_and_duplicate_transactions_do_not_count():
    other = helius._encode(bytes([9]) * 32)
    duplicate = transaction(1)
    duplicate["transaction"]["message"]["instructions"] *= 3
    entries = [
        duplicate,
        transaction(2, mint=other),
        transaction(3, pool=other),
        transaction(4, seconds=901),
        transaction(5, seconds=-1),
        transaction(1),
    ]
    result = evidence.enrich_candidates(
        [candidate()], at=AT, policy=Policy(), rpc=lambda _: page(entries)
    )[0]
    assert result["memecoin_assessment"]["sentiment"]["observed_swaps"] == 1
    assert "Sample at least three recent swaps" in entry_reasons(result, Policy(), AT)


def test_successful_empty_page_is_checked_but_does_not_pass_or_repeat():
    calls = []

    def rpc(request):
        calls.append(request)
        return page([])

    result = evidence.enrich_candidates([candidate()], at=AT, policy=Policy(), rpc=rpc)[0]
    assert result["memecoin_assessment"]["coverage"]["swap_state"] == "empty"
    assert not result.get("_trading_evidence_deferred")
    assert entry_reasons(result, Policy(), AT) == ["Sample at least three recent swaps"]
    evidence.enrich_candidates(
        [candidate()], at=AT + timedelta(seconds=59), policy=Policy(), rpc=rpc
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error", [ValueError("provider URL is secret"), store.CreditBudgetReached("budget")]
)
def test_failures_are_cached_deferred_and_do_not_freshen_coverage(error):
    calls = []

    def rpc(request):
        calls.append(request)
        raise error

    for seconds in (0, 30):
        result = evidence.enrich_candidates(
            [candidate()], at=AT + timedelta(seconds=seconds), policy=Policy(), rpc=rpc
        )[0]
        assert result["_trading_evidence_deferred"]
        assert result["memecoin_assessment"]["coverage"]["swap_checked_at"] is None
        assert "secret" not in str(result)
    assert len(calls) == 1


def test_bounded_fairness_puts_unchecked_pool_before_retry():
    rows = [
        candidate(helius._encode(bytes([i]) * 32), helius._encode(bytes([i + 10]) * 32))
        for i in range(2, 5)
    ]
    calls = []

    def rpc(request):
        calls.append(request["params"][0])
        raise ValueError("retry")

    first = evidence.enrich_candidates(rows, at=AT, policy=Policy(), rpc=rpc)
    assert len(calls) == 2 and all(row["_trading_evidence_deferred"] for row in first)
    evidence.enrich_candidates(rows, at=AT + timedelta(seconds=61), policy=Policy(), rpc=rpc)
    assert len(calls) == 4 and calls[2] == rows[2]["pool_address"]


def test_static_risk_gates_prevent_paid_work_and_global_refresh_is_not_token_coverage():
    row = candidate()
    row["memecoin_assessment"]["sentiment"]["observed_swaps"] = 20
    assert entry_reasons(row, Policy(), AT) == ["Refresh this pool's swap evidence"]
    for field, value in [
        ("ratification", {"ratified": False}),
        ("trading_checks", {}),
        ("real_liquidity_usd", 100),
    ]:
        blocked = {**row, field: value}
        assert non_swap_entry_reasons(blocked, Policy(), AT)
        result = evidence.enrich_candidates(
            [blocked], at=AT, policy=Policy(), rpc=lambda _: pytest.fail("paid read")
        )
        assert result == [blocked]


def test_stale_foreign_cache_and_expired_swaps_never_pass():
    evidence.enrich_candidates(
        [candidate()],
        at=AT,
        policy=Policy(),
        rpc=lambda _: page([transaction(i, seconds=850 + i) for i in (1, 2, 3)]),
    )
    expired = evidence.apply_cached_evidence(candidate(), at=AT + timedelta(seconds=60))
    assert expired["memecoin_assessment"]["sentiment"]["observed_swaps"] == 0
    stale = evidence.apply_cached_evidence(candidate(), at=AT + timedelta(seconds=301))
    assert stale["memecoin_assessment"]["coverage"]["swap_checked_at"] is None
    stream = evidence._stream(candidate())
    state = deepcopy(store.stream_state(stream))
    state["sample"]["mint"] = POOL
    store.save_batch(stream, [], [], state, at=AT)
    foreign = evidence.apply_cached_evidence(candidate(), at=AT)
    assert foreign["memecoin_assessment"]["coverage"]["swap_checked_at"] is None
