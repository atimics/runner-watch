from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web import main as web_main
from runner_web.caller_ids import (
    MACHINE_HANDLE,
    MACHINE_USER_ID,
    ensure_caller_identity_with_database,
    ensure_machine_trader,
)
from runner_web.db import connection, init_db
from runner_web.flash_wallet import credit_flash


def _database(tmp_path: Path, monkeypatch: MonkeyPatch) -> datetime:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "caller-board.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    init_db()
    current = datetime.now(UTC)
    with connection() as database:
        for user_id in ("fresh", "mature", "quiet"):
            database.execute(
                "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
                (user_id, user_id, user_id.title(), "active", current.isoformat()),
            )
        ensure_machine_trader(database)
    return current


def _identity_handle(user_id: str) -> str:
    with connection() as database:
        return str(
            database.execute(
                "SELECT handle FROM caller_identities WHERE user_id=?", (user_id,)
            ).fetchone()["handle"]
        )


def _settled_stock_call(
    user_id: str,
    ticker: str,
    *,
    entry_price: float,
    exit_price: float,
    exit_at: str = "2026-01-01T19:00:00+00:00",
) -> None:
    with connection() as database:
        identity = ensure_caller_identity_with_database(database, user_id)
        call_id = f"{user_id}-{ticker}-{exit_at}"
        database.execute(
            """
            INSERT INTO community_calls(
                id,public_id,user_id,caller_identity_id,ticker,side,entry_price,entry_at,
                exit_price,exit_at,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'long',?,?,?,?,?,?,?)
            """,
            (
                call_id,
                call_id,
                user_id,
                identity["id"],
                ticker,
                entry_price,
                "2026-01-01T14:00:00+00:00",
                exit_price,
                exit_at,
                "closed",
                exit_at,
                exit_at,
            ),
        )


def _machine_settled_call(entry_price: float, exit_price: float, exit_at: str) -> None:
    with connection() as database:
        identity = database.execute(
            "SELECT id FROM caller_identities WHERE handle=?", (MACHINE_HANDLE,)
        ).fetchone()
        call_id = f"machine-{exit_at}"
        database.execute(
            """
            INSERT INTO community_calls(
                id,public_id,user_id,caller_identity_id,ticker,side,entry_price,entry_at,
                exit_price,exit_at,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'long',?,?,?,?,?,?,?)
            """,
            (
                call_id,
                call_id,
                MACHINE_USER_ID,
                identity["id"],
                "AAA",
                entry_price,
                "2026-01-01T13:30:00+00:00",
                exit_price,
                exit_at,
                "closed",
                exit_at,
                exit_at,
            ),
        )


def _win_flash(user_id: str, amount: int, *, at: datetime, kind: str = "runner_call_win") -> None:
    with connection() as database:
        assert credit_flash(
            database, user_id, amount, kind=kind, reference_id=f"{user_id}-{amount}-{at}", at=at
        )[1]


def test_leaderboard_ranks_flash_earned_and_gates_on_settled_calls(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    recent = current - timedelta(days=1)
    # mature: five settled stock calls (meets the maturity gate), wins across markets
    for ticker in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        _settled_stock_call("mature", ticker, entry_price=10.0, exit_price=10.4)
    _win_flash("mature", 50, at=recent)
    _win_flash("mature", 25, at=recent, kind="sports_call_win")
    # quiet: one settled call and one small win - below the 5 settled gate
    _settled_stock_call("quiet", "AAA", entry_price=10.0, exit_price=10.2)
    _win_flash("quiet", 5, at=recent)
    # fresh: earned Flash this week, more than mature, but zero settled calls
    _win_flash("fresh", 400, at=recent)

    board = web_main.callers_leaderboard(at=current)

    assert [row["handle"] for row in board["rows"]] == [_identity_handle("mature")]
    assert board["rows"][0]["flash_earned"] == 75
    assert board["rows"][0]["win_count"] == 2
    assert board["min_settled"] == 5


def test_leaderboard_window_excludes_old_earnings(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    for ticker in ("AAA", "BBB", "CCC", "DDD", "EEE"):
        _settled_stock_call("mature", ticker, entry_price=10.0, exit_price=10.1)
    _win_flash("mature", 50, at=current - timedelta(days=9))
    _win_flash("mature", 20, at=current - timedelta(days=1))

    board = web_main.callers_leaderboard(at=current)

    assert [row["handle"] for row in board["rows"]] == [_identity_handle("mature")]
    assert board["rows"][0]["flash_earned"] == 20


def test_machine_benchmark_reports_its_window_record(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch)
    _machine_settled_call(10.0, 11.0, (current - timedelta(days=1)).isoformat())
    _machine_settled_call(10.0, 9.5, (current - timedelta(days=2)).isoformat())
    _machine_settled_call(10.0, 10.3, (current - timedelta(days=9)).isoformat())

    board = web_main.callers_leaderboard(at=current)

    assert board["machine"]["settled"] == 2
    assert board["machine"]["wins"] == 1
    assert board["machine"]["losses"] == 1
    assert board["machine"]["avg_return_pct"] == 2.5
    assert board["rows"] == []


def test_alpha_board_carries_the_callers_leaderboard(tmp_path, monkeypatch) -> None:
    _database(tmp_path, monkeypatch)

    board = web_main.alpha_board_data()

    assert "callers" in board
    assert board["callers"]["rows"] == []
    ledger = (Path(__file__).parents[1] / "web/templates/_alpha_ledger.html").read_text()
    assert "Top callers" in ledger
    assert "earns no Flash" in ledger
    assert "Rankings need" in ledger