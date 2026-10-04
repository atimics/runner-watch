from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from runner_web import db, main
from runner_web import memecoin_evidence as evidence
from runner_web import onchain_wallets as wallets
from runner_web import wallet_backfill as backfill
from runner_web.helius_discovery import _encode
from runner_web.wallet_registry import register_chain
from tests.test_onchain_wallets import ADDRESS, AT, MINT, QUOTES, download, fake_rpc, transaction
from tests.test_onchain_wallets import database as database


def state():
    with db.connection() as connection:
        row = connection.execute(
            "SELECT state_json FROM onchain_wallet_backfills WHERE address=?", (ADDRESS,)
        ).fetchone()
    return json.loads(row["state_json"])


def paged_rpc(seen):
    original = fake_rpc(seen)

    def rpc(body, *, credits):
        if body["method"] == "getTransactionsForAddress":
            seen.append((body, credits))
            cursor = body["params"][1].get("paginationToken")
            return {
                "result": {
                    "data": [transaction(1, 0, 100, -2)]
                    if not cursor
                    else [transaction(2, 100, 50, 2)],
                    "paginationToken": "101:1" if not cursor else None,
                }
            }
        reply = original(body, credits=credits)
        if body["method"] == "getTokenAccountsByOwner" and reply["result"]["value"]:
            reply["result"]["value"][0]["account"]["data"]["parsed"]["info"]["tokenAmount"][
                "amount"
            ] = "50"
        return reply

    return rpc


def test_buying_cost_and_fees_survive_pages_duplicates_and_a_new_window(database):
    buy, sell, final = (
        transaction(1, 0, 100, -2),
        transaction(2, 100, 50, 2),
        transaction(3, 50, 0, 2),
    )
    whole = wallets.calculate_pnl(ADDRESS, [final, sell, buy], {}, QUOTES)
    first = wallets.calculate_pnl(
        ADDRESS, [buy], {MINT: Decimal(50)}, QUOTES, keep_checkpoint=True, history_complete=False
    )
    assert first["holdings"][0]["cost"] is None
    assert first["pnl"][0]["unrealized"] is None
    second = wallets.calculate_pnl(
        ADDRESS,
        [buy, sell],
        {MINT: Decimal(50)},
        QUOTES,
        checkpoint=first["_checkpoint"],
        keep_checkpoint=True,
    )
    assert second["pnl"][0]["realized"] == 1
    assert second["holdings"][0]["cost"] == 1
    third = wallets.calculate_pnl(
        ADDRESS, [sell, final], {}, QUOTES, checkpoint=second["_checkpoint"], keep_checkpoint=True
    )
    for key in ("pnl", "fees_sol", "trades", "receipts", "transactions", "oldest_at", "newest_at"):
        assert third[key] == whole[key]
    assert third["_checkpoint"]["positions"] == {}


def test_same_slot_page_boundary_and_provider_order_keep_costs(database):
    buy, sell = transaction(1, 0, 100, -2), transaction(2, 100, 0, 3)
    sell["slot"] = buy["slot"]
    first = wallets.calculate_pnl(ADDRESS, [buy], {}, QUOTES, keep_checkpoint=True)
    second = wallets.calculate_pnl(
        ADDRESS, [buy, sell], {}, QUOTES, checkpoint=first["_checkpoint"], keep_checkpoint=True
    )
    assert second["pnl"][0]["realized"] == 1
    assert second["transactions"] == 2
    assert len(second["_checkpoint"]["tail"]) == 2


def test_saved_cursor_keeps_window_and_costs_across_refreshes(database):
    seen = []
    rpc = paged_rpc(seen)
    first = wallets.refresh_wallet(ADDRESS, at=AT, rpc=rpc, download=download)
    assert first["transactions"] == 1
    assert first["backfill"]["status"] == "loading"
    assert "_state" not in first
    first_state = state()
    # A later read resumes the committed cursor and frozen upper time bound.
    second = wallets.refresh_wallet(
        ADDRESS, at=AT + timedelta(minutes=1), rpc=rpc, download=download
    )
    assert second["transactions"] == 2
    assert second["pnl"][0]["realized"] == 1
    assert second["holdings"][0]["cost"] == 1
    assert second["backfill"]["status"] == "complete"
    requests = [
        body["params"][1] for body, _ in seen if body["method"] == "getTransactionsForAddress"
    ]
    assert requests[1]["paginationToken"] == first_state["cursor"]
    assert requests[1]["filters"] == requests[0]["filters"]
    assert state()["book"]["transactions"] == 2
    summary = (
        TestClient(main.app)
        .get("/api/wallets/" + register_chain(ADDRESS) + "/pnl?summary=true")
        .json()
    )
    assert summary["backfill"]["status"] == "complete"
    assert set(summary) == {"status", "error", "updated_at", "backfill"}


def test_failed_page_keeps_cursor_and_earlier_realized_pnl(database):
    rpc = paged_rpc([])
    wallets.refresh_wallet(ADDRESS, at=AT, rpc=rpc, download=download)
    before = state()

    def failed(body, *, credits):
        if body["method"] == "getTransactionsForAddress":
            assert body["params"][1]["paginationToken"] == before["cursor"]
            raise ValueError("https://provider.invalid/?api-key=secret")
        return rpc(body, credits=credits)

    value = wallets.refresh_wallet(
        ADDRESS, at=AT + timedelta(minutes=1), rpc=failed, download=download
    )
    assert state() == before
    assert value["transactions"] == 1
    assert "secret" not in json.dumps(value)
    recovered = wallets.refresh_wallet(
        ADDRESS, at=AT + timedelta(minutes=2), rpc=rpc, download=download
    )
    assert recovered["transactions"] == 2
    assert recovered["error"] is None


def test_cursor_and_snapshot_rollback_together(database, monkeypatch):
    rpc = paged_rpc([])
    wallets.refresh_wallet(ADDRESS, at=AT, rpc=rpc, download=download)
    before = state()
    original = backfill.connection

    @contextmanager
    def broken_save():
        with original() as connection:

            class FailSnapshot:
                def execute(self, statement, parameters):
                    if statement.startswith("INSERT INTO onchain_wallet_snapshots"):
                        raise RuntimeError("database write failed")
                    return connection.execute(statement, parameters)

            yield FailSnapshot()

    monkeypatch.setattr(backfill, "connection", broken_save)
    with pytest.raises(RuntimeError, match="write failed"):
        wallets.refresh_wallet(ADDRESS, at=AT + timedelta(minutes=1), rpc=rpc, download=download)
    assert state() == before
    assert wallets.saved_wallet(ADDRESS, at=AT)["transactions"] == 1
    monkeypatch.setattr(backfill, "connection", original)
    recovered = wallets.refresh_wallet(
        ADDRESS, at=AT + timedelta(minutes=7), rpc=rpc, download=download
    )
    assert recovered["transactions"] == 2
    assert recovered["pnl"][0]["realized"] == 1


@pytest.mark.parametrize("max_pages", [1, 5])
def test_expired_claim_cannot_replace_newer_saved_data(database, monkeypatch, max_pages):
    original = wallets.collect_wallet

    def collect(address, *, at, **kwargs):
        if at == AT:
            wallets.refresh_wallet(
                ADDRESS, at=AT + timedelta(seconds=301), rpc=fake_rpc([]), download=download
            )
        result = original(address, at=at, **kwargs)
        if at == AT:
            result["balance_sol"] = 999
        return result

    monkeypatch.setattr(wallets, "collect_wallet", collect)
    result = wallets.refresh_wallet(
        ADDRESS, at=AT, rpc=paged_rpc([]), download=download, max_pages=max_pages
    )
    assert result["balance_sol"] == 8
    assert result["updated_at"] == (AT + timedelta(seconds=301)).isoformat()


def test_only_opened_wallets_are_read_and_oldest_due_wallet_goes_first(database, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "test")
    wallets.wallet_catalog()
    assert backfill.refresh_opened_wallet(at=AT) is None
    other = _encode(bytes([9]) * 32)
    with db.connection() as connection:
        for address, age in ((ADDRESS, 2), (other, 1)):
            connection.execute(
                "INSERT INTO onchain_wallet_snapshots(address,refresh_after,updated_at) "
                "VALUES(?,?,?)",
                (address, AT.isoformat(), (AT - timedelta(minutes=age)).isoformat()),
            )
    original = wallets.refresh_wallet
    seen = []

    def refresh(address, *, at):
        seen.append(address)
        return original(address, at=at, rpc=fake_rpc([]), download=download)

    monkeypatch.setattr(wallets, "refresh_wallet", refresh)
    backfill.refresh_opened_wallet(at=AT)
    backfill.refresh_opened_wallet(at=AT)
    assert seen == [ADDRESS, other]
    assert backfill.refresh_opened_wallet(at=AT) is None


def test_rpc_reads_overlap_without_changing_their_credit_ceiling():
    barrier = threading.Barrier(4)
    original = fake_rpc([])

    def rpc(body, *, credits):
        barrier.wait(timeout=3)
        return original(body, credits=credits)

    result = wallets.collect_wallet(ADDRESS, at=AT, rpc=rpc, download=download)
    assert result["holdings"][0]["cost"] == 2


def test_a_full_thousand_record_page_is_saved_and_the_next_page_adds_more(database):
    original = fake_rpc([])
    rows = []
    for index in range(1001):
        row = transaction(1, 100, 100, 0, transfer=True)
        row["transaction"]["signatures"] = [_encode((index + 1000).to_bytes(64, "big"))]
        row.update(
            slot=index + 1000, transactionIndex=0, blockTime=int(AT.timestamp()) - 2000 + index
        )
        rows.append(row)

    def rpc(body, *, credits):
        if body["method"] == "getTransactionsForAddress":
            cursor = body["params"][1].get("paginationToken")
            return {
                "result": {
                    "data": rows[:1000] if not cursor else rows[1000:],
                    "paginationToken": "1999:0" if not cursor else None,
                }
            }
        return original(body, credits=credits)

    first = wallets.refresh_wallet(ADDRESS, at=AT, rpc=rpc, download=download)
    assert first["transactions"] == 1000
    assert len(first["receipts"]) == 200
    second = wallets.refresh_wallet(
        ADDRESS, at=AT + timedelta(minutes=1), rpc=rpc, download=download
    )
    assert second["transactions"] == 1001
    assert second["fees_sol"] == pytest.approx(0.005005)
    assert len(second["receipts"]) == 200
    assert len(json.dumps(state())) < 100_000


@pytest.mark.parametrize(
    ("daily_limit", "wallet_limit", "other_limit"), [(10000, 2000, 6000), (30000, 7000, 21000)]
)
def test_wallet_allowance_is_atomic_and_preserves_discovery_and_prices(
    database, monkeypatch, daily_limit, wallet_limit, other_limit
):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("HELIUS_DAILY_CREDITS", str(daily_limit))

    def reserve(_):
        try:
            evidence.reserve_credits(100, at=AT, lane="wallet")
            return True
        except evidence.CreditBudgetReached:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(wallet_limit // 100 + 4))) == wallet_limit // 100
    evidence.reserve_credits(other_limit, at=AT)
    evidence.reserve_credits(2000, at=AT, lane="price")
    status = evidence.budget_status(at=AT)
    assert status["reserved_credits"] == daily_limit
    assert status["wallet_credits"] == status["wallet_daily_limit"] == wallet_limit


def test_budget_increase_resumes_saved_wallet_before_midnight(database, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "test")
    monkeypatch.setenv("HELIUS_DAILY_CREDITS", "10000")
    rpc = paged_rpc([])
    wallets.refresh_wallet(ADDRESS, at=AT, rpc=rpc, download=download)
    before = state()
    evidence.reserve_credits(1941, at=AT, lane="wallet")

    def exhausted(*_, **__):
        raise evidence.CreditBudgetReached("budget")

    wallets.refresh_wallet(ADDRESS, at=AT + timedelta(minutes=1), rpc=exhausted, download=download)
    assert backfill.refresh_opened_wallet(at=AT + timedelta(minutes=2)) is None
    assert state() == before
    monkeypatch.setenv("HELIUS_DAILY_CREDITS", "30000")
    original = wallets.refresh_wallet

    def refresh(address, *, at):
        return original(address, at=at, rpc=rpc, download=download)

    monkeypatch.setattr(wallets, "refresh_wallet", refresh)
    recovered = backfill.refresh_opened_wallet(at=AT + timedelta(minutes=2))
    assert recovered["transactions"] == 2
    assert recovered["pnl"][0]["realized"] == 1
    assert recovered["error"] is None
    assert recovered["backfill"]["status"] == "complete"


def test_budget_pause_waits_for_price_room_and_keeps_other_retries(database, monkeypatch):
    monkeypatch.setenv("HELIUS_DAILY_CREDITS", "10000")
    future = AT + timedelta(hours=1)
    other = _encode(bytes([9]) * 32)
    leased = _encode(bytes([10]) * 32)
    with db.connection() as connection:
        for address, error, lease in (
            (ADDRESS, backfill.BUDGET_WAIT_MESSAGE, AT),
            (other, "Chain data is temporarily unavailable. Try again in a minute.", AT),
            (leased, backfill.BUDGET_WAIT_MESSAGE, future),
        ):
            connection.execute(
                "INSERT INTO onchain_wallet_backfills"
                "(address,state_json,next_read_at,lease_until,error,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (address, "{}", future.isoformat(), lease.isoformat(), error, AT.isoformat()),
            )
    evidence.reserve_credits(7900, at=AT)
    backfill.resume_budget_waits(at=AT)
    with db.connection() as connection:
        assert all(
            row["next_read_at"] == future.isoformat()
            for row in connection.execute("SELECT next_read_at FROM onchain_wallet_backfills")
        )
    monkeypatch.setenv("HELIUS_DAILY_CREDITS", "30000")
    backfill.resume_budget_waits(at=AT)
    with db.connection() as connection:
        jobs = {
            row["address"]: row["next_read_at"]
            for row in connection.execute(
                "SELECT address,next_read_at FROM onchain_wallet_backfills"
            )
        }
    assert jobs[ADDRESS] == AT.isoformat()
    assert jobs[other] == future.isoformat()
    assert jobs[leased] == future.isoformat()


def test_short_page_settles_on_charged_day_and_budget_wait_keeps_progress(database, monkeypatch):
    def provider(body, *, credits, lane, at):
        evidence.reserve_credits(credits, at=at, lane=lane)
        return {"result": {"data": [transaction(1, 0, 100, -2)]}}

    monkeypatch.setattr(wallets, "rpc_request", provider)
    wallets.wallet_rpc({"method": "getTransactionsForAddress"}, credits=100)
    today = datetime.now(UTC)
    assert evidence.budget_status(at=today)["wallet_credits"] == 10
    evidence.reserve_credits(100, at=AT, lane="wallet")
    evidence.release_wallet_credits(90, at=AT)
    assert evidence.budget_status(at=AT)["wallet_credits"] == (
        20 if today.date() == AT.date() else 10
    )
    wallets.refresh_wallet(ADDRESS, at=AT, rpc=paged_rpc([]), download=download)
    before = state()

    def exhausted(*_, **__):
        raise evidence.CreditBudgetReached("budget")

    result = wallets.refresh_wallet(
        ADDRESS, at=AT + timedelta(minutes=1), rpc=exhausted, download=download
    )
    assert state() == before
    assert result["transactions"] == 1
    assert "budget resets" in result["error"]
    next_day = (AT + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    assert result["backfill"]["next_read_at"] == next_day.isoformat()


def batch_rpc(seen, *, fail_page=None):
    original = fake_rpc(seen)

    def rpc(body, *, credits):
        if body["method"] != "getTransactionsForAddress":
            return original(body, credits=credits)
        seen.append((body, credits))
        cursor = body["params"][1].get("paginationToken")
        index = int(cursor.split(":")[0]) - 100 if cursor else 1
        if index == fail_page:
            raise evidence.CreditBudgetReached("budget")
        return {
            "result": {
                "data": [
                    transaction(index, 0 if index == 1 else 100, 100, -2 if index == 1 else 0)
                ],
                "paginationToken": f"{101 + index}:0" if index < 6 else None,
            }
        }

    return rpc


def test_five_page_batch_shares_reads_and_matches_separate_pages(database):
    seen, price_reads = [], []

    def prices(*args):
        price_reads.append(args)
        return download(*args)

    result = wallets.refresh_wallet(
        ADDRESS, at=AT, rpc=batch_rpc(seen), download=prices, max_pages=5
    )
    assert result["transactions"] == 5
    assert result["backfill"]["status"] == "loading"
    assert len(price_reads) == 1
    assert sum(credits for _, credits in seen) == 503
    assert sum(body["method"] == "getBalance" for body, _ in seen) == 1
    assert sum(body["method"] == "getTokenAccountsByOwner" for body, _ in seen) == 2
    requests = [
        body["params"][1] for body, _ in seen if body["method"] == "getTransactionsForAddress"
    ]
    assert [row.get("paginationToken") for row in requests] == [
        None,
        "102:0",
        "103:0",
        "104:0",
        "105:0",
    ]
    assert all(row["filters"] == requests[0]["filters"] for row in requests)
    assert "read_cache" not in json.dumps(state())

    # The next batch fetches fresh balances and quotes, then stops on completion.
    final = wallets.refresh_wallet(
        ADDRESS, at=AT + timedelta(minutes=1), rpc=batch_rpc(seen), download=prices, max_pages=5
    )
    assert final["transactions"] == 6
    assert final["backfill"]["status"] == "complete"
    assert len(price_reads) == 2
    whole = wallets.calculate_pnl(
        ADDRESS,
        [transaction(i, 0 if i == 1 else 100, 100, -2 if i == 1 else 0) for i in range(1, 7)],
        {MINT: Decimal(100)},
        QUOTES,
    )
    for key in ("pnl", "fees_sol", "holdings", "transactions", "receipts"):
        assert final[key] == whole[key]


def test_batch_commits_each_page_while_one_claim_remains_active(database):
    original = batch_rpc([])

    def rpc(body, *, credits):
        if body["method"] == "getTransactionsForAddress":
            cursor = body["params"][1].get("paginationToken")
            if cursor:
                with db.connection() as connection:
                    row = connection.execute(
                        "SELECT lease_token FROM onchain_wallet_backfills WHERE address=?",
                        (ADDRESS,),
                    ).fetchone()
                assert row["lease_token"]
                assert state()["cursor"] == cursor
                assert (
                    wallets.saved_wallet(ADDRESS, at=AT)["transactions"]
                    == int(cursor.split(":")[0]) - 101
                )
                # A second reader sees the committed page under the active claim.
                competing = []
                wallets.refresh_wallet(
                    ADDRESS, at=AT, rpc=fake_rpc(competing), download=download, max_pages=5
                )
                assert competing == []
        return original(body, credits=credits)

    result = wallets.refresh_wallet(ADDRESS, at=AT, rpc=rpc, download=download, max_pages=5)
    assert result["transactions"] == 5
    with db.connection() as connection:
        assert (
            connection.execute(
                "SELECT lease_token FROM onchain_wallet_backfills WHERE address=?", (ADDRESS,)
            ).fetchone()["lease_token"]
            is None
        )


def test_budget_stops_a_batch_after_committed_pages_and_resume_uses_next_cursor(database):
    seen = []
    result = wallets.refresh_wallet(
        ADDRESS, at=AT, rpc=batch_rpc(seen, fail_page=3), download=download, max_pages=5
    )
    assert result["transactions"] == state()["book"]["transactions"] == 2
    assert state()["cursor"] == "103:0"
    assert result["error"] == backfill.BUDGET_WAIT_MESSAGE
    reset = (AT + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    assert result["backfill"]["next_read_at"] == reset.isoformat()
    assert len([body for body, _ in seen if body["method"] == "getTransactionsForAddress"]) == 3
    resumed = wallets.refresh_wallet(
        ADDRESS, at=reset, rpc=batch_rpc([]), download=download, max_pages=5
    )
    assert resumed["transactions"] == 6
    assert resumed["holdings"][0]["cost"] == 2
    assert resumed["error"] is None


def test_provider_error_keeps_completed_pages_and_masks_private_error(database):
    original = batch_rpc([])

    def rpc(body, *, credits):
        if body["method"] == "getTransactionsForAddress" and body["params"][1].get(
            "paginationToken"
        ):
            raise ValueError("https://provider.invalid/?api-key=secret")
        return original(body, credits=credits)

    result = wallets.refresh_wallet(ADDRESS, at=AT, rpc=rpc, download=download, max_pages=5)
    assert result["transactions"] == 1
    assert state()["cursor"] == "102:0"
    assert "secret" not in json.dumps(result)
    resumed = wallets.refresh_wallet(
        ADDRESS, at=AT + timedelta(minutes=1), rpc=batch_rpc([]), download=download, max_pages=5
    )
    assert resumed["transactions"] == 6


def test_batch_time_bound_yields_after_saved_page(database, monkeypatch):
    ticks = iter([0, backfill.BATCH_SECONDS])
    monkeypatch.setattr(backfill, "monotonic", lambda: next(ticks))
    result = wallets.refresh_wallet(
        ADDRESS, at=AT, rpc=batch_rpc([]), download=download, max_pages=5
    )
    assert result["transactions"] == 1
    assert state()["cursor"] == "102:0"


@pytest.mark.parametrize("pages", [0, 6, True, 1.5])
def test_batch_size_is_bounded_before_paid_reads(database, pages):
    seen = []
    with pytest.raises(ValueError, match="one to five"):
        wallets.refresh_wallet(
            ADDRESS, at=AT, rpc=fake_rpc(seen), download=download, max_pages=pages
        )
    assert seen == []


def test_worker_batches_two_oldest_opened_wallets_in_parallel(database, monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "test")
    addresses = [ADDRESS, _encode(bytes([9]) * 32), _encode(bytes([10]) * 32)]
    with db.connection() as connection:
        for index, address in enumerate(addresses):
            connection.execute(
                "INSERT INTO onchain_wallet_snapshots"
                "(address,refresh_after,updated_at) VALUES(?,?,?)",
                (address, AT.isoformat(), (AT + timedelta(seconds=index)).isoformat()),
            )
    barrier = threading.Barrier(2)
    seen = []

    def refresh(address, *, at, max_pages):
        barrier.wait(timeout=3)
        seen.append((address, at, max_pages))
        return {"address": address}

    monkeypatch.setattr(wallets, "refresh_wallet", refresh)
    assert backfill.refresh_opened_wallets(at=AT) == [
        {"address": address} for address in addresses[:2]
    ]
    assert {address for address, _, _ in seen} == set(addresses[:2])
    assert all(at == AT and pages == 5 for _, at, pages in seen)


def test_batch_honors_wallet_allowance_and_keeps_discovery_and_price_room(database, monkeypatch):
    monkeypatch.setenv("HELIUS_DAILY_CREDITS", "30000")
    evidence.reserve_credits(6875, at=AT, lane="wallet")
    original = batch_rpc([])

    def rpc(body, *, credits):
        evidence.reserve_credits(credits, at=AT, lane="wallet")
        return original(body, credits=credits)

    result = wallets.refresh_wallet(ADDRESS, at=AT, rpc=rpc, download=download, max_pages=5)
    assert result["transactions"] == 1
    assert state()["cursor"] == "102:0"
    assert result["error"] == backfill.BUDGET_WAIT_MESSAGE
    assert evidence.budget_status(at=AT)["wallet_credits"] == 6978
    evidence.reserve_credits(21000, at=AT)
    evidence.reserve_credits(2000, at=AT, lane="price")
    assert evidence.budget_status(at=AT)["reserved_credits"] == 29978


def test_batch_cache_is_scoped_to_one_wallet(database):
    cache = {}
    wallets.collect_wallet(ADDRESS, at=AT, rpc=fake_rpc([]), download=download, read_cache=cache)
    seen = []
    with pytest.raises(ValueError, match="share one address"):
        wallets.collect_wallet(
            _encode(bytes([9]) * 32), at=AT, rpc=fake_rpc(seen), download=download, read_cache=cache
        )
    assert seen == []
