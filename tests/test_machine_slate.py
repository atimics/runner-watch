from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.caller_ids import MACHINE_HANDLE, MACHINE_USER_ID
from runner_web.calls import create_call, open_machine_slate, settle_stock_calls
from runner_web.db import connection, init_db
from runner_web.flash_wallet import wallet_for_user

SESSION_DAY = "2026-09-04"
SESSION_OPEN = datetime(2026, 9, 4, 13, 30, tzinfo=UTC)
SESSION_CLOSE = datetime(2026, 9, 4, 20, 0, tzinfo=UTC)
LATE_MORNING = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
AFTER_CLOSE = SESSION_CLOSE + timedelta(hours=2)


def _database(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "machine-slate.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    init_db()


def _report_and_forecasts(ticker: str, direction: str) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_session_reports(
                id,report_day,report_type,source_scan_run_id,as_of,headline,summary,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT DO NOTHING
            """,
            ("report-1", SESSION_DAY, "pre_market", "scan-1", SESSION_OPEN.isoformat(), "h", "s",
             SESSION_OPEN.isoformat(), SESSION_OPEN.isoformat()),
        )
        database.execute(
            """
            INSERT INTO market_report_forecasts(
                report_id,ticker,report_day,reference_price,reference_at,target_price,
                direction,reason,model,contract_version,forecast_at,status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT DO NOTHING
            """,
            (
                "report-1",
                ticker,
                SESSION_DAY,
                10.0,
                SESSION_OPEN.isoformat(),
                10.5,
                direction,
                "test forecast",
                "test-model",
                "premarket-eod-target-v1",
                SESSION_OPEN.isoformat(),
                "pending",
            ),
        )


def _snapshot(ticker: str, price: float, quote_time: str) -> None:
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
                f"snapshot-{ticker}-{quote_time}",
                ticker,
                price,
                quote_time,
                quote_time,
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


def _machine_calls() -> list[dict]:
    with connection() as database:
        return [
            dict(row)
            for row in database.execute(
                "SELECT ticker,entry_price,entry_at,status FROM community_calls WHERE user_id=?",
                (MACHINE_USER_ID,),
            ).fetchall()
        ]


def test_machine_opens_its_up_picks_at_the_first_session_quote(tmp_path, monkeypatch) -> None:
    _database(tmp_path, monkeypatch)
    _report_and_forecasts("ONE", "up")
    _report_and_forecasts("TWO", "up")
    _report_and_forecasts("DOWN", "down")
    _snapshot("ONE", 10.2, (SESSION_OPEN + timedelta(minutes=10)).isoformat())
    _snapshot("ONE", 10.8, (SESSION_OPEN + timedelta(minutes=40)).isoformat())
    _snapshot("TWO", 20.0, (SESSION_OPEN + timedelta(hours=2)).isoformat())

    opened = open_machine_slate(at=LATE_MORNING)

    assert opened == ["ONE"]
    calls = _machine_calls()
    assert len(calls) == 1
    assert calls[0]["ticker"] == "ONE"
    assert calls[0]["entry_price"] == 10.2
    assert calls[0]["entry_at"] == (SESSION_OPEN + timedelta(minutes=10)).isoformat()

    with connection() as database:
        identity = database.execute(
            "SELECT handle FROM caller_identities WHERE handle=?", (MACHINE_HANDLE,)
        ).fetchone()
    assert identity is not None

    assert open_machine_slate(at=LATE_MORNING + timedelta(minutes=30)) == []
    assert len(_machine_calls()) == 1


def test_machine_waits_for_the_open(tmp_path, monkeypatch) -> None:
    _database(tmp_path, monkeypatch)
    _report_and_forecasts("ONE", "up")
    _snapshot("ONE", 10.2, (SESSION_OPEN + timedelta(minutes=10)).isoformat())

    assert open_machine_slate(at=SESSION_OPEN - timedelta(hours=2)) == []
    assert _machine_calls() == []


def test_machine_falls_back_to_the_official_open_after_the_close(tmp_path, monkeypatch) -> None:
    _database(tmp_path, monkeypatch)
    _report_and_forecasts("ONE", "up")
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,open,high,low,close,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "massive",
                "ONE",
                "1d",
                SESSION_DAY,
                9.9,
                11.0,
                9.5,
                10.4,
                AFTER_CLOSE.isoformat(),
                AFTER_CLOSE.isoformat(),
            ),
        )

    opened = open_machine_slate(at=AFTER_CLOSE)

    assert opened == ["ONE"]
    calls = _machine_calls()
    assert calls[0]["entry_price"] == 9.9
    assert calls[0]["entry_at"] == SESSION_OPEN.isoformat()


def test_machine_skips_picks_without_any_price(tmp_path, monkeypatch) -> None:
    _database(tmp_path, monkeypatch)
    _report_and_forecasts("ONE", "up")

    assert open_machine_slate(at=AFTER_CLOSE) == []
    assert _machine_calls() == []


def test_machine_settlement_earns_no_flash_but_the_record_counts(tmp_path, monkeypatch) -> None:
    _database(tmp_path, monkeypatch)
    _report_and_forecasts("ONE", "up")
    _snapshot("ONE", 10.0, SESSION_OPEN.isoformat())
    _snapshot("ONE", 11.0, (SESSION_CLOSE - timedelta(minutes=5)).isoformat())
    opened = open_machine_slate(at=LATE_MORNING)
    assert opened == ["ONE"]

    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("human", "human", "Human", "active", LATE_MORNING.isoformat()),
        )
    human = create_call(
        "human",
        "ONE",
        entry_price=10.0,
        entry_at=(SESSION_OPEN + timedelta(minutes=30)).isoformat(),
    )
    assert human is not None

    handles = settle_stock_calls(at=AFTER_CLOSE)

    with connection() as database:
        rows = database.execute(
            "SELECT user_id,status,exit_price FROM community_calls ORDER BY user_id"
        ).fetchall()
        credits = database.execute(
            "SELECT user_id,amount FROM flash_transactions WHERE kind='runner_call_win'"
        ).fetchall()
    by_user = {row["user_id"]: row for row in rows}
    assert by_user[MACHINE_USER_ID]["status"] == "closed"
    assert by_user[MACHINE_USER_ID]["exit_price"] == 11.0
    assert by_user["human"]["status"] == "closed"
    assert [dict(row) for row in credits] == [{"user_id": "human", "amount": 50}]
    assert wallet_for_user(MACHINE_USER_ID)["balance"] == 0
    assert wallet_for_user("human")["balance"] == 50
    assert MACHINE_HANDLE in handles