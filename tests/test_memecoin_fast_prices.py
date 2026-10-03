import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from runner_web import db, memecoin_fast_prices, memecoin_store, memecoins
from runner_web.db import connection, init_db
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
