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


def test_expired_claim_cannot_replace_newer_saved_data(database, monkeypatch):
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
    result = wallets.refresh_wallet(ADDRESS, at=AT, rpc=fake_rpc([]), download=download)
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


def test_wallet_allowance_is_atomic_and_preserves_discovery_and_prices(database, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setenv("HELIUS_DAILY_CREDITS", "10000")

    def reserve(_):
        try:
            evidence.reserve_credits(100, at=AT, lane="wallet")
            return True
        except evidence.CreditBudgetReached:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(24))) == 20
    evidence.reserve_credits(6000, at=AT)
    evidence.reserve_credits(2000, at=AT, lane="price")
    status = evidence.budget_status(at=AT)
    assert status["reserved_credits"] == 10000
    assert status["wallet_credits"] == status["wallet_daily_limit"] == 2000


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
