from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.calls import (
    active_call_for_user,
    call_for_user,
    call_stats,
    calls_for_ticker,
    close_call,
    create_call,
)
from runner_web.db import connection, init_db
from runner_web.flash_wallet import CALL_WIN_FLASH_CAP, runner_call_reward, wallet_for_user


def test_runner_call_reward_is_ten_times_positive_pnl_up_to_the_cap() -> None:
    assert runner_call_reward(None) == 0
    assert runner_call_reward(-1) == 0
    assert runner_call_reward(0) == 0
    assert runner_call_reward(0.01) == 0
    assert runner_call_reward(1.25) == 13
    assert runner_call_reward(10) == CALL_WIN_FLASH_CAP
    assert runner_call_reward(50) == CALL_WIN_FLASH_CAP


def test_public_call_freezes_entry_and_exit_marks(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "positions.db")
    init_db()
    current = datetime.now(UTC)
    entered_at = (current - timedelta(days=2)).isoformat()
    exited_at = (current - timedelta(hours=1)).isoformat()
    with connection() as database:
        database.executemany(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            [
                ("owner", "owner", "Owner", "active", current.isoformat()),
                ("other", "other", "Other", "active", current.isoformat()),
            ],
        )
    created = create_call(
        "owner",
        "ONE",
        entry_price=2.0,
        entry_at=entered_at,
    )

    assert created["entry_at"] == entered_at
    active = active_call_for_user("owner", "ONE", current_price=2.5)
    assert active["return_pct"] == 25.0
    assert active["flash_reward"] == 0
    assert active["projected_flash_reward"] == CALL_WIN_FLASH_CAP
    caller_handle = calls_for_ticker("ONE", current_price=2.5)[0]["caller_handle"]
    assert "-" in caller_handle
    assert "user_id" not in calls_for_ticker("ONE", current_price=2.5)[0]
    assert active_call_for_user("other", "ONE", current_price=2.5) is None
    assert call_for_user("other", created["public_id"]) is None

    closed = close_call(
        "owner",
        created["public_id"],
        exit_price=3.0,
        exit_at=exited_at,
    )

    assert closed is not None
    assert closed["status"] == "closed"
    assert closed["exit_at"] == exited_at
    assert closed["return_pct"] == 50.0
    assert closed["flash_reward"] == CALL_WIN_FLASH_CAP
    assert closed["projected_flash_reward"] == CALL_WIN_FLASH_CAP
    assert closed["reward_label"] == f"+{CALL_WIN_FLASH_CAP} Flash"
    assert wallet_for_user("owner")["balance"] == CALL_WIN_FLASH_CAP
    assert call_stats([closed])["wins"] == 1
    assert (
        close_call("owner", created["public_id"], exit_price=3.1, exit_at=current.isoformat())
        is None
    )
    assert wallet_for_user("owner")["balance"] == CALL_WIN_FLASH_CAP

    losing = create_call(
        "other",
        "TWO",
        entry_price=2.0,
        entry_at=entered_at,
    )
    lost = close_call(
        "other",
        losing["public_id"],
        exit_price=1.0,
        exit_at=exited_at,
    )
    assert lost is not None
    assert lost["flash_reward"] == 0
    assert lost["reward_label"] is None
    assert wallet_for_user("other")["balance"] == 0

    with connection() as database:
        rewards = database.execute(
            "SELECT user_id,amount,kind FROM flash_transactions ORDER BY created_at"
        ).fetchall()
    assert [tuple(row) for row in rewards] == [("owner", CALL_WIN_FLASH_CAP, "runner_call_win")]


def test_trade_pages_use_ranked_alpha_and_pulse_radar() -> None:
    root = Path(__file__).parents[1]
    ticker = (root / "web/templates/ticker.html").read_text()
    ticker_script = (root / "web/static/ticker-detail.js").read_text()
    flash_action = (root / "web/templates/_flash_report_action.html").read_text()
    flash_script = (root / "web/static/flash-report.js").read_text()
    alpha = (root / "web/templates/community.html").read_text()
    alpha += (root / "web/templates/_alpha_ledger.html").read_text()
    navigation = (root / "web/templates/mobile_base.html").read_text()
    app_source = (root / "src/runner_web/main.py").read_text()
    radar = (root / "web/templates/radar.html").read_text()

    assert "Make Call" in ticker
    assert "Post as" not in ticker
    assert "callerIdentity" not in ticker
    assert "swapCommentAlias" not in ticker
    assert "Swap this thread" not in ticker
    assert "/api/caller-identities" not in app_source
    assert "/alias/swap" not in app_source
    assert "Public · stamped" in ticker
    assert "Entry time" not in ticker
    assert "Add exit" not in ticker
    assert "flash_report_action(flash_report)" in ticker
    assert "const REPORT_COST = 100" in flash_script
    assert "commissionButton" in flash_action
    assert "Flash earned" in ticker_script
    assert "call.reward_label" in alpha
    assert "flash.model" not in ticker
    assert "🐺" in alpha
    assert "open Calls" in alpha
    assert "data-alpha-reaction" not in alpha
    assert "call.return_pct" in alpha
    assert "heart" not in alpha.lower()
    assert "heartButton" not in ticker
    assert '<span class="tab-icon alpha-icon" aria-hidden="true"></span>' in navigation
    assert "My Calls" in navigation
    assert '@app.post("/api/calls/{ticker}")' in app_source
    assert '@app.post("/api/positions/' not in app_source
    assert '@app.post("/api/heart/{ticker}")' not in app_source
    assert "My Radar" not in radar
    assert "events from Pulse" in radar
