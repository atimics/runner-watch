import json
from datetime import timedelta
from unittest.mock import patch

from runner_trading import ledger
from runner_trading.config import Policy
from runner_web import deployment_check, memecoin_fast_prices, memecoin_store, memecoins
from runner_web.db import connection
from runner_web.memecoin_calls import expire_memecoin_calls
from tests.test_call_expiry import _database, _quote_history, _stale_memecoin_call
from tests.test_memecoin_fast_prices import AT, fast_chain, seed_board
from tests.test_memecoin_fast_prices import market_db as market_db


def test_open_calls_receive_prices_after_leaving_the_board(market_db, monkeypatch):
    seed_board()
    monkeypatch.setattr(memecoin_fast_prices, "_call_coin_ids", lambda: ["dogecoin"])
    memecoins._save_state("memecoins_snapshot", {"rows": []}, AT)
    memecoins.note_memecoin_view(AT - timedelta(days=1))
    with patch.object(
        memecoin_fast_prices, "chain_prices", return_value=fast_chain(0.15)
    ) as reader:
        result = memecoin_fast_prices.refresh_watched_prices(at=AT)
    assert result["count"] == 1
    assert reader.call_args.args[0][0]["pool_address"]


def test_unresolved_expiry_has_a_visible_reason_and_can_recover(tmp_path, monkeypatch):
    now = _database(tmp_path, monkeypatch)
    _stale_memecoin_call(0.12, (now - timedelta(days=9)).isoformat())
    assert expire_memecoin_calls(at=now) == []
    with connection() as db:
        call = db.execute("SELECT * FROM memecoin_calls").fetchone()
        assert call["resolution_state"] == "awaiting_quote"
        db.execute(
            "INSERT INTO memecoin_assets VALUES(?,?,?,?)",
            (
                "dogecoin",
                json.dumps({"price": 0.15, "observed_at": (now - timedelta(days=8)).isoformat()}),
                now.isoformat(),
                "stored-run",
            ),
        )
    assert len(expire_memecoin_calls(at=now)) == 1
    with connection() as db:
        assert db.execute("SELECT resolution_state FROM memecoin_calls").fetchone()[0] == "ok"


def test_quote_pruning_keeps_evidence_for_open_calls(tmp_path, monkeypatch):
    now = _database(tmp_path, monkeypatch)
    _stale_memecoin_call(0.12, (now - timedelta(days=9)).isoformat())
    _quote_history(0.15, (now - timedelta(days=8)).isoformat())
    memecoin_store.save_memecoin_snapshot([], run_id="latest", collected_at=now)
    with connection() as db:
        assert db.execute("SELECT COUNT(*) FROM memecoin_quote_history").fetchone()[0] == 1
    assert len(expire_memecoin_calls(at=now)) == 1


def test_active_trader_keeps_market_sampling_and_is_a_release_requirement(market_db, monkeypatch):
    memecoins.note_memecoin_view(AT - timedelta(days=1))
    assert memecoins.memecoins_quiet(AT)
    identifier = ledger.ensure_account("paper", Policy(), AT)["id"]
    with connection() as db:
        db.execute(
            "UPDATE trading_accounts SET heartbeat_at=? WHERE id=?", (AT.isoformat(), identifier)
        )
    assert not memecoins.memecoins_quiet(AT)
    ledger.pause(identifier, "Operator paused entries", AT)
    assert memecoins.memecoins_quiet(AT)
    monkeypatch.setenv("TRADING_EXPECTED", "1")
    assert deployment_check.background_processes_healthy() is False
