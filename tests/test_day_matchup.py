from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web import main as web_main
from runner_web.caller_ids import MACHINE_HANDLE, ensure_machine_trader
from runner_web.calls import close_call, create_call
from runner_web.db import connection, init_db


def _database(tmp_path: Path, monkeypatch: MonkeyPatch) -> datetime:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "day-matchup.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    init_db()
    current = datetime.now(UTC)
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("human", "human", "Human", "active", current.isoformat()),
        )
        ensure_machine_trader(database)
    return current


def _human_handle() -> str:
    with connection() as database:
        return str(
            database.execute(
                "SELECT handle FROM caller_identities WHERE user_id='human'"
            ).fetchone()["handle"]
        )


def _closed_call(
    user_id: str,
    entry_price: float,
    exit_price: float,
    *,
    entry_at: str,
    exit_at: str,
    ticker: str,
) -> None:
    created = create_call(user_id, ticker, entry_price=entry_price, entry_at=entry_at)
    assert created is not None
    closed = close_call(user_id, created["public_id"], exit_price=exit_price, exit_at=exit_at)
    assert closed is not None


def test_day_verdict_compares_settled_todays_stock_calls(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    today = current.strftime("%Y-%m-%d")
    _closed_call(
        "human", 10.0, 11.0,
        entry_at=f"{today}T14:00:00+00:00",
        exit_at=f"{today}T19:00:00+00:00",
        ticker="AAA",
    )
    _closed_call(
        MACHINE_HANDLE, 10.0, 10.2,
        entry_at=f"{today}T14:00:00+00:00",
        exit_at=f"{today}T19:00:00+00:00",
        ticker="BBB",
    )
    _closed_call(
        MACHINE_HANDLE, 10.0, 9.9,
        entry_at=f"{today}T14:00:00+00:00",
        exit_at=f"{today}T19:30:00+00:00",
        ticker="CCC",
    )
    # Yesterday's settlement does not count.
    yesterday = (current - timedelta(days=1)).strftime("%Y-%m-%d")
    _closed_call(
        MACHINE_HANDLE, 10.0, 20.0, entry_at=f"{yesterday}T14:00:00+00:00",
        exit_at=f"{yesterday}T19:00:00+00:00", ticker="DDD",
    )

    record = web_main._unified_caller_page_data(_human_handle())

    today_block = record["today"]
    assert today_block["you"]["settled"] == 1
    assert today_block["you"]["wins"] == 1
    assert today_block["you"]["losses"] == 0
    assert today_block["you"]["avg_return_pct"] == 10.0
    assert today_block["machine"]["settled"] == 2
    assert today_block["machine"]["wins"] == 1
    assert today_block["machine"]["losses"] == 1
    assert today_block["machine"]["avg_return_pct"] == 0.5
    assert today_block["verdict"] == "you"


def test_day_verdict_reports_the_machine_when_the_user_has_not_settled(
    tmp_path, monkeypatch
) -> None:
    current = _database(tmp_path, monkeypatch)
    today = current.strftime("%Y-%m-%d")
    _closed_call(
        MACHINE_HANDLE, 10.0, 11.0,
        entry_at=f"{today}T14:00:00+00:00",
        exit_at=f"{today}T19:00:00+00:00",
        ticker="AAA",
    )
    create_call("human", "BBB", entry_price=10.0, entry_at=f"{today}T15:00:00+00:00")

    record = web_main._unified_caller_page_data(_human_handle())

    assert record["today"]["you"]["settled"] == 0
    assert record["today"]["machine"]["settled"] == 1
    assert record["today"]["verdict"] == "machine-only"


def test_win_streak_counts_consecutive_settled_wins(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    today = current.strftime("%Y-%m-%d")
    _closed_call(
        "human", 10.0, 10.5,
        entry_at=f"{today}T14:00:00+00:00",
        exit_at=f"{today}T15:00:00+00:00",
        ticker="AAA",
    )
    _closed_call(
        "human", 10.0, 10.4,
        entry_at=f"{today}T15:00:00+00:00",
        exit_at=f"{today}T16:00:00+00:00",
        ticker="BBB",
    )
    _closed_call(
        "human", 10.0, 9.5,
        entry_at=f"{today}T16:00:00+00:00",
        exit_at=f"{today}T17:00:00+00:00",
        ticker="CCC",
    )
    _closed_call(
        "human", 10.0, 10.1,
        entry_at=f"{today}T17:00:00+00:00",
        exit_at=f"{today}T18:00:00+00:00",
        ticker="DDD",
    )
    create_call("human", "EEE", entry_price=10.0, entry_at=f"{today}T18:30:00+00:00")

    record = web_main._unified_caller_page_data(_human_handle())

    assert record["streak"] == 1  # the loss broke the streak; the last win started a new one
    assert record["today"]["you"]["settled"] == 4
    assert record["today"]["you"]["wins"] == 3


def test_the_machine_page_compares_itself_honestly(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    today = current.strftime("%Y-%m-%d")
    _closed_call(
        MACHINE_HANDLE, 10.0, 11.0,
        entry_at=f"{today}T14:00:00+00:00",
        exit_at=f"{today}T19:00:00+00:00",
        ticker="AAA",
    )

    record = web_main._unified_caller_page_data(MACHINE_HANDLE)

    assert record["today"]["you"]["settled"] == 1
    assert record["today"]["machine"]["settled"] == 1
    assert record["today"]["verdict"] == "even"