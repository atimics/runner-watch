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


def test_public_call_freezes_entry_and_exit_marks(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
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
    assert active_call_for_user("owner", "ONE", current_price=2.5)["return_pct"] == 25.0
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
    assert call_stats([closed])["wins"] == 1
    assert close_call(
        "owner", created["public_id"], exit_price=3.1, exit_at=current.isoformat()
    ) is None


def test_trade_pages_use_ranked_alpha_and_pulse_radar() -> None:
    root = Path(__file__).parents[1]
    ticker = (root / "web/templates/ticker.html").read_text()
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
    assert "Generate today's report" in ticker
    assert "100 Flash" in ticker
    assert "flash.model" not in ticker
    assert "🐺" in alpha
    assert "open Calls" in alpha
    assert "data-alpha-reaction" not in alpha
    assert "call.return_pct" in alpha
    assert "heart" not in alpha.lower()
    assert "heartButton" not in ticker
    assert '<span class="tab-icon alpha-icon" aria-hidden="true">🐺</span>' in navigation
    assert "My Calls" in navigation
    assert '@app.post("/api/calls/{ticker}")' in app_source
    assert '@app.post("/api/positions/' not in app_source
    assert '@app.post("/api/heart/{ticker}")' not in app_source
    assert "My Radar" not in radar
    assert "events from Pulse" in radar
