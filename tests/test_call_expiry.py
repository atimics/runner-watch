from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db, operations
from runner_web.caller_ids import ensure_caller_identity_with_database
from runner_web.calls import close_call, create_call, expire_stock_calls
from runner_web.db import connection, init_db
from runner_web.flash_wallet import CALL_WIN_FLASH_CAP, wallet_for_user
from runner_web.memecoin_calls import expire_memecoin_calls


def _database(tmp_path: Path, monkeypatch: MonkeyPatch) -> datetime:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "call-expiry.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    init_db()
    current = datetime.now(UTC)
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("owner", "owner", "Owner", "active", current.isoformat()),
        )
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("later", "later", "Later", "active", current.isoformat()),
        )
    return current


def _snapshot(price: float, captured_at: str, ticker: str = "ONE") -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,price,captured_at,quote_time,stage,session,score,change_pct,
                momentum_5m_pct,momentum_15m_pct,breakout_pct,dollar_volume,
                signals_json,risks_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"snapshot-{captured_at}",
                ticker,
                price,
                captured_at,
                captured_at,
                "watch",
                "test",
                50.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1_000_000.0,
                "[]",
                "[]",
            ),
        )


def _stale_memecoin_call(entry_price: float, entry_at: str) -> str:
    with connection() as database:
        identity = ensure_caller_identity_with_database(database, "owner")
        database.execute(
            """
            INSERT INTO memecoin_calls(
                public_id,user_id,caller_identity_id,coin_id,symbol,name,status,
                entry_price,entry_at,entry_evidence,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?,?,?,?)
            """,
            (
                "meme-expiry",
                "owner",
                identity["id"],
                "dogecoin",
                "DOGE",
                "Dogecoin",
                entry_price,
                entry_at,
                "{}",
                entry_at,
                entry_at,
            ),
        )
    return "meme-expiry"


def _quote_history(price: float, observed_at: str, coin_id: str = "dogecoin") -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO memecoin_assets(coin_id,quote_json,collected_at,run_id)
            VALUES(?,?,?,?) ON CONFLICT DO NOTHING
            """,
            (coin_id, "{}", observed_at, "run-1"),
        )
        database.execute(
            """
            INSERT INTO memecoin_quote_history(
                coin_id,observed_at,collected_at,price,run_id
            ) VALUES(?,?,?,?,?)
            """,
            (coin_id, observed_at, observed_at, price, "run-1"),
        )


def _stock_call(entry_price: float, entry_at: str, ticker: str = "ONE") -> str:
    created = create_call("owner", ticker, entry_price=entry_price, entry_at=entry_at)
    return str(created["public_id"])


def test_stale_winning_stock_call_settles_at_the_latest_mark(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    entered_at = (current - timedelta(days=40)).isoformat()
    marked_at = (current - timedelta(days=39)).isoformat()
    public_id = _stock_call(2.0, entered_at)
    _snapshot(3.0, marked_at)

    handles = expire_stock_calls(at=current)

    with connection() as database:
        row = database.execute(
            "SELECT status,exit_price,exit_at FROM community_calls WHERE public_id=?",
            (public_id,),
        ).fetchone()
        identity = database.execute(
            "SELECT handle FROM caller_identities WHERE user_id='owner'"
        ).fetchone()
    assert handles == [str(identity["handle"])]
    assert row["status"] == "closed"
    assert row["exit_price"] == 3.0
    assert row["exit_at"] == marked_at
    assert wallet_for_user("owner")["balance"] == CALL_WIN_FLASH_CAP


def test_stale_losing_stock_call_records_the_loss_without_credit(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    entered_at = (current - timedelta(days=40)).isoformat()
    marked_at = (current - timedelta(days=39)).isoformat()
    public_id = _stock_call(2.0, entered_at)
    _snapshot(1.5, marked_at)

    assert expire_stock_calls(at=current) != []

    with connection() as database:
        row = database.execute(
            "SELECT status,exit_price FROM community_calls WHERE public_id=?",
            (public_id,),
        ).fetchone()
    assert row["status"] == "closed"
    assert row["exit_price"] == 1.5
    assert wallet_for_user("owner")["balance"] == 0


def test_fresh_stock_calls_stay_open(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    entered_at = (current - timedelta(days=2)).isoformat()
    public_id = _stock_call(2.0, entered_at)
    _snapshot(2.5, (current - timedelta(days=1)).isoformat())

    assert expire_stock_calls(at=current) == []

    with connection() as database:
        status = database.execute(
            "SELECT status FROM community_calls WHERE public_id=?", (public_id,)
        ).fetchone()["status"]
    assert status == "active"


def test_stale_call_without_a_mark_at_or_after_entry_stays_open(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    entered_at = (current - timedelta(days=40)).isoformat()
    public_id = _stock_call(2.0, entered_at)
    _snapshot(2.5, (current - timedelta(days=41)).isoformat())

    assert expire_stock_calls(at=current) == []

    with connection() as database:
        status = database.execute(
            "SELECT status FROM community_calls WHERE public_id=?", (public_id,)
        ).fetchone()["status"]
    assert status == "active"


def test_expiry_sweeps_are_idempotent(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    entered_at = (current - timedelta(days=40)).isoformat()
    _snapshot(2.5, (current - timedelta(days=39)).isoformat())
    _stock_call(2.0, entered_at)

    assert expire_stock_calls(at=current) != []
    assert expire_stock_calls(at=current) == []
    with connection() as database:
        credits = database.execute(
            "SELECT COUNT(*) AS count FROM flash_transactions WHERE kind='runner_call_win'"
        ).fetchone()["count"]
    assert credits == 1


def test_manual_close_still_wins_the_race_against_the_sweeper(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    entered_at = (current - timedelta(days=40)).isoformat()
    public_id = _stock_call(2.0, entered_at)
    _snapshot(3.0, (current - timedelta(days=39)).isoformat())
    closed = close_call("owner", public_id, exit_price=2.4, exit_at=current.isoformat())

    assert closed is not None
    assert expire_stock_calls(at=current) == []
    with connection() as database:
        row = database.execute(
            "SELECT exit_price FROM community_calls WHERE public_id=?", (public_id,)
        ).fetchone()
    assert row["exit_price"] == 2.4


def test_stale_winning_memecoin_call_settles_at_the_latest_stored_quote(
    tmp_path, monkeypatch
) -> None:
    current = _database(tmp_path, monkeypatch)
    entered_at = (current - timedelta(days=8)).isoformat()
    observed_at = (current - timedelta(days=7)).isoformat()
    _stale_memecoin_call(0.12, entered_at)
    _quote_history(0.15, observed_at)

    handles = expire_memecoin_calls(at=current)

    assert len(handles) == 1
    with connection() as database:
        row = database.execute(
            "SELECT status,exit_price,exit_at,exit_evidence FROM memecoin_calls"
        ).fetchone()
    assert row["status"] == "closed"
    assert row["exit_price"] == 0.15
    assert row["exit_at"] == observed_at
    evidence = json.loads(row["exit_evidence"])
    assert evidence["auto_expired"] is True
    assert evidence["run_id"] == "run-1"
    assert wallet_for_user("owner")["balance"] == 25
    assert expire_memecoin_calls(at=current) == []
    assert wallet_for_user("owner")["balance"] == 25


def test_worker_contract_requires_the_call_expiry_sweeper() -> None:
    required = operations.required_worker_names(sports_ingestion_enabled=False)

    assert "call-expiry" in required
