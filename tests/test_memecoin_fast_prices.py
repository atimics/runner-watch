import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from runner_web import db, memecoin_fast_prices, memecoin_store, memecoins
from runner_web import main as web_main
from runner_web import memecoin_calls as call_service
from runner_web.db import connection, init_db
from runner_web.flash_wallet import MEMECOIN_CALL_STAKE, credit_flash
from runner_web.memecoin_evidence import CreditBudgetReached

AT = datetime(2026, 9, 5, 7, tzinfo=UTC)
POOL = "PoolAddress1111111111111111111111111111111111"


@pytest.fixture
def market_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "fast.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setenv("MEMECOINS_ENABLED", "true")
    monkeypatch.setenv("MEMECOIN_PRICE_SOURCE", "chain")
    init_db()


def seed_board():
    """One saved coin, quoted at AT, with the receipt the fast loop reads."""

    row = memecoins.normalize_memecoins(
        [
            {
                "id": "dogecoin",
                "symbol": "doge",
                "name": "Dogecoin",
                "current_price": 0.12,
                "market_cap": 1_000_000,
                "total_volume": 50_000,
                "price_change_percentage_24h": 2.5,
                "last_updated": AT.isoformat(),
            }
        ]
    )[0]
    row.update(
        pool_address=POOL,
        venue="pool",
        discovery={"pool_address": POOL, "token_address": "Token", "venue": "pool"},
    )
    memecoin_store.save_memecoin_snapshot([row], run_id="run", collected_at=AT)
    memecoins._save_state("memecoin_pool_watch", [row["discovery"]], AT)
    return row


def fast_chain(price):
    return {"sol_usd": 100.0, "prices": {POOL: {"price": price, "liquidity_usd": 50_000.0}}}


@pytest.fixture
def paper_clock(market_db, monkeypatch):
    clock = {"now": AT}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    for module in (call_service, memecoins, memecoin_fast_prices):
        monkeypatch.setattr(module, "datetime", Clock)
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("alice", "alice", "Alice", "active", AT.isoformat()),
        )
        credit_flash(database, "alice", 100, kind="test_grant", reference_id="grant-alice")
    seed_board()
    call_service.create_memecoin_call("alice", "dogecoin")
    return clock


def fast_tick(monkeypatch):
    """Run one complete worker tick, including its fill and cache invalidation."""

    async def run_task(function):
        return function()

    async def sleep(seconds):
        assert seconds == memecoin_fast_prices.FAST_PRICE_SECONDS
        raise asyncio.CancelledError

    with monkeypatch.context() as patcher:
        patcher.setattr(web_main, "run_in_threadpool", run_task)
        patcher.setattr(web_main.asyncio, "sleep", sleep)
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(web_main.memecoin_fast_price_worker())


def test_unattended_call_fills_on_first_fast_tick_without_slow_refresh(paper_clock, monkeypatch):
    # The latest board has lost this coin. Its saved receipt still supports the Call.
    memecoin_store.save_memecoin_snapshot([], run_id="later", collected_at=AT)
    paper_clock["now"] += timedelta(seconds=memecoin_fast_prices.FAST_PRICE_SECONDS)
    first_quote_at = paper_clock["now"]
    with (
        patch.object(memecoin_fast_prices, "chain_prices", return_value=fast_chain(0.15)) as read,
        patch.object(web_main, "refresh_memecoins") as slow,
        patch.object(web_main, "_invalidate_public_screen_data") as invalidate,
    ):
        fast_tick(monkeypatch)
        opened = call_service.active_memecoin_call("alice", "dogecoin")
        assert opened["entry_at"] == first_quote_at.isoformat()
        assert opened["entry_price"] == 0.15
        assert opened["entry_evidence"]["observed_at"] == first_quote_at.isoformat()
        invalidate.assert_called_once_with("caller", opened["caller_handle"])
        # Retrying the same sample and a later tick cannot create a second Call or refund.
        fast_tick(monkeypatch)
        paper_clock["now"] += timedelta(seconds=memecoin_fast_prices.FAST_PRICE_SECONDS)
        fast_tick(monkeypatch)
        assert read.call_count == 3  # Active Calls keep their prices refreshed too.
        slow.assert_not_called()
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM memecoin_calls").fetchone()[0] == 1
        assert database.execute("SELECT balance FROM flash_wallets").fetchone()[0] == (
            100 - MEMECOIN_CALL_STAKE
        )
        assert (
            database.execute(
                "SELECT COUNT(*) FROM flash_transactions WHERE kind='memecoin_call_refund'"
            ).fetchone()[0]
            == 0
        )


def test_fast_tick_does_not_fill_at_quote_already_seen_when_order_was_requested(
    paper_clock, monkeypatch
):
    with patch.object(memecoin_fast_prices, "chain_prices", return_value=fast_chain(0.15)):
        fast_tick(monkeypatch)  # Same timestamp as the order and existing slow quote.
    assert call_service.active_memecoin_call("alice", "dogecoin") is None
    assert call_service.pending_memecoin_order("alice", "dogecoin") is not None


def test_unattended_close_uses_next_fast_quote_then_goes_idle(paper_clock, monkeypatch):
    paper_clock["now"] += timedelta(seconds=memecoin_fast_prices.FAST_PRICE_SECONDS)
    with patch.object(memecoin_fast_prices, "chain_prices", return_value=fast_chain(0.15)):
        fast_tick(monkeypatch)
    opened = call_service.active_memecoin_call("alice", "dogecoin")
    call_service.close_memecoin_call("alice", opened["public_id"])
    paper_clock["now"] += timedelta(seconds=memecoin_fast_prices.FAST_PRICE_SECONDS)
    with patch.object(memecoin_fast_prices, "chain_prices", return_value=fast_chain(0.18)) as read:
        fast_tick(monkeypatch)
        fast_tick(monkeypatch)
        read.assert_called_once()
    with connection() as database:
        call = database.execute("SELECT * FROM memecoin_calls").fetchone()
        assert call["status"] == "closed"
        assert call["exit_price"] == 0.18
        assert (
            database.execute(
                "SELECT COUNT(*) FROM flash_transactions WHERE kind='memecoin_call_settle'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("failure", [CreditBudgetReached("spent"), ValueError("down"), None])
def test_unattended_failed_or_empty_read_retries_without_filling(paper_clock, monkeypatch, failure):
    paper_clock["now"] += timedelta(seconds=memecoin_fast_prices.FAST_PRICE_SECONDS)
    with (
        patch.object(
            memecoin_fast_prices, "chain_prices", side_effect=failure, return_value={"prices": {}}
        ),
        patch.object(web_main, "fill_memecoin_call_orders") as fill,
    ):
        fast_tick(monkeypatch)
        fill.assert_not_called()
    assert call_service.pending_memecoin_order("alice", "dogecoin") is not None
    paper_clock["now"] += timedelta(seconds=memecoin_fast_prices.FAST_PRICE_SECONDS)
    with patch.object(memecoin_fast_prices, "chain_prices", return_value=fast_chain(0.16)):
        fast_tick(monkeypatch)
    assert call_service.active_memecoin_call("alice", "dogecoin")["entry_price"] == 0.16


def test_unattended_watch_only_reads_calls_and_retains_its_cap(market_db):
    row = seed_board()
    calls = [{**row, "id": f"call-{i}", "pool_address": f"pool-{i}"} for i in range(50)]
    with (
        patch.object(memecoin_fast_prices, "_call_coin_ids", return_value=[r["id"] for r in calls]),
        patch.object(memecoin_fast_prices, "chain_prices", return_value={"prices": {}}) as read,
    ):
        memecoin_store.save_memecoin_snapshot([row, *calls], run_id="many", collected_at=AT)
        assert memecoin_fast_prices.refresh_watched_prices(at=AT)["status"] == "ok"
        selected = memecoin_fast_prices.watched_rows([row, *calls], include_traded=False)
    assert len(selected) == memecoin_fast_prices.WATCH_LIMIT
    assert all(r["id"] != "dogecoin" for r in selected)
    assert len(read.call_args.args[0]) == memecoin_fast_prices.WATCH_LIMIT


def test_newer_fast_price_replaces_price_and_scales_market_cap():
    row = {"id": "a", "price": 2.0, "market_cap": 100.0, "observed_at": AT.isoformat()}
    fast = {
        "prices": {
            "a": {"price": 3.0, "liquidity_usd": 9.0, "observed_at": "2026-09-05T07:05:00+00:00"}
        }
    }
    merged, read_at = memecoin_store.with_fast_price(row, fast, AT.isoformat())
    assert merged["price"] == 3.0
    assert merged["market_cap"] == 150.0
    assert merged["liquidity_usd"] == 9.0
    assert read_at == "2026-09-05T07:05:00+00:00"


@pytest.mark.parametrize(
    "entry",
    [
        {"price": 3.0, "observed_at": "2026-09-05T06:00:00+00:00"},  # older than the row
        {"price": -1, "observed_at": "2026-09-05T07:05:00+00:00"},
        {"price": "bad", "observed_at": "2026-09-05T07:05:00+00:00"},
        {"observed_at": "2026-09-05T07:05:00+00:00"},
    ],
)
def test_older_or_broken_fast_price_is_ignored(entry):
    row = {"id": "a", "price": 2.0, "market_cap": 100.0, "observed_at": AT.isoformat()}
    merged, read_at = memecoin_store.with_fast_price(row, {"prices": {"a": entry}}, "saved")
    assert merged is row
    assert read_at == "saved"


def test_fast_loop_keeps_watched_coin_fresh_while_snapshot_is_old(market_db):
    seed_board()
    memecoins.note_memecoin_view(AT + timedelta(minutes=14))
    now = AT + timedelta(minutes=14, seconds=30)
    with patch.object(memecoin_fast_prices, "chain_prices", return_value=fast_chain(0.15)):
        result = memecoin_fast_prices.refresh_watched_prices(at=now, rpc=lambda *a, **k: {})
    assert result == {"status": "ok", "count": 1}
    later = AT + timedelta(minutes=20)
    market = memecoins.memecoin_market(at=later)
    assert market["rows"][0]["price"] == 0.15
    assert market["rows"][0]["stale"] is False
    assert market["status"] == "ok"
    assert market["collected_at"] == now.isoformat()
    with connection() as database:
        points = database.execute(
            "SELECT price FROM memecoin_quote_history WHERE coin_id='dogecoin' ORDER BY observed_at"
        ).fetchall()
    assert [point["price"] for point in points] == [0.12, 0.15]
    detail = memecoins.memecoin_detail("dogecoin", at=later)
    assert detail["coin"]["price"] == 0.15
    assert detail["status"] == "ok"


def test_unwatched_board_still_goes_stale_without_a_fast_price(market_db):
    seed_board()
    assert memecoins.memecoin_market(at=AT + timedelta(minutes=20))["status"] == "stale"


def test_fast_loop_idles_when_nobody_is_reading(market_db):
    seed_board()
    with patch.object(memecoin_fast_prices, "chain_prices") as read:
        result = memecoin_fast_prices.refresh_watched_prices(at=AT, rpc=lambda *a, **k: {})
    assert result == {"status": "idle"}
    read.assert_not_called()
    memecoins.note_memecoin_view(AT - timedelta(minutes=10))
    with patch.object(memecoin_fast_prices, "chain_prices") as read:
        assert memecoin_fast_prices.refresh_watched_prices(at=AT)["status"] == "idle"
    read.assert_not_called()


def test_fast_loop_skips_thin_pools_and_survives_budget_and_errors(market_db):
    seed_board()
    memecoins.note_memecoin_view(AT)
    thin = {"sol_usd": 100.0, "prices": {POOL: {"price": 0.2, "liquidity_usd": 10.0}}}
    with patch.object(memecoin_fast_prices, "chain_prices", return_value=thin):
        assert memecoin_fast_prices.refresh_watched_prices(at=AT)["count"] == 0
    for failure, status in ((CreditBudgetReached("spent"), "budget"), (ValueError("x"), "error")):
        with patch.object(memecoin_fast_prices, "chain_prices", side_effect=failure):
            assert memecoin_fast_prices.refresh_watched_prices(at=AT)["status"] == status
    assert memecoins.memecoin_market(at=AT)["rows"][0]["price"] == 0.12


def test_open_calls_are_watched_even_when_the_coin_did_not_trade(market_db):
    row = seed_board()
    memecoins._save_state("memecoin_pool_watch", [], AT)
    assert memecoin_fast_prices.watched_rows([row]) == []
    with patch.object(memecoin_fast_prices, "_call_coin_ids", return_value=["dogecoin"]):
        assert [item["id"] for item in memecoin_fast_prices.watched_rows([row])] == ["dogecoin"]


def test_refresh_logs_where_the_time_went(market_db, caplog):
    body = json.dumps([]).encode()
    with (
        caplog.at_level("WARNING", logger="runner_web.memecoins"),
        patch.object(memecoins, "_collect_helius", side_effect=ValueError("down")),
    ):
        memecoins.refresh_memecoins(download=lambda *_: body, at=AT)
    assert any("memecoin_refresh_stages status=error" in message for message in caplog.messages)


def history_times():
    with connection() as database:
        return [
            row["observed_at"]
            for row in database.execute(
                "SELECT observed_at FROM memecoin_quote_history ORDER BY observed_at"
            ).fetchall()
        ]


def test_fast_history_keeps_five_minute_spacing_unless_an_order_waits(market_db):
    seed_board()
    for minute in (1, 2, 6):
        at = AT + timedelta(minutes=minute)
        memecoin_store.save_fast_quotes([{"coin_id": "dogecoin", "price": 0.2}], observed_at=at)
    # AT itself is the slow loop's point; one minute and two minutes are too close.
    assert history_times() == [AT.isoformat(), (AT + timedelta(minutes=6)).isoformat()]
    memecoin_store.save_fast_quotes(
        [{"coin_id": "dogecoin", "price": 0.2}],
        observed_at=AT + timedelta(minutes=7),
        every_tick={"dogecoin"},
    )
    assert len(history_times()) == 3


def test_history_cap_keeps_the_newest_points(market_db, monkeypatch):
    seed_board()
    monkeypatch.setattr(memecoin_store, "MAX_HISTORY_POINTS", 5)
    with connection() as database:
        for minute in range(1, 11):
            database.execute(
                "INSERT INTO memecoin_quote_history(coin_id,observed_at,collected_at,price,run_id) "
                "VALUES('dogecoin',?,?,0.1,'run')",
                ((AT + timedelta(minutes=minute)).isoformat(),) * 2,
            )
    memecoins_row = memecoins.normalize_memecoins(
        [
            {
                "id": "dogecoin",
                "symbol": "doge",
                "name": "Dogecoin",
                "current_price": 0.12,
                "last_updated": (AT + timedelta(minutes=11)).isoformat(),
            }
        ]
    )
    memecoin_store.save_memecoin_snapshot(
        memecoins_row, run_id="run2", collected_at=AT + timedelta(minutes=11)
    )
    kept = history_times()
    assert len(kept) == 5
    assert kept[-1] == (AT + timedelta(minutes=11)).isoformat()


def test_slow_snapshot_save_logs_its_steps(market_db, caplog, monkeypatch):
    ticks = iter(range(0, 1000, 20))
    monkeypatch.setattr(memecoin_store.time, "monotonic", lambda: next(ticks))
    row = seed_board()
    with caplog.at_level("WARNING", logger="runner_web.memecoin_store"):
        memecoin_store.save_memecoin_snapshot([row], run_id="slow", collected_at=AT)
    assert any("memecoin_snapshot_save slow" in m and "prune_cap=" in m for m in caplog.messages)
