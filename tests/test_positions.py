from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.positions import (
    close_position,
    create_position,
    position_for_user,
    positions_for_ticker,
)


def test_private_position_keeps_historical_entry_and_exit_times(
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

    created = create_position(
        "owner",
        "ONE",
        entry_price=2.0,
        entry_at=entered_at,
    )

    assert created["entry_at"] == entered_at
    assert positions_for_ticker("owner", "ONE", current_price=2.5)[0]["return_pct"] == 25.0
    assert positions_for_ticker("other", "ONE", current_price=2.5) == []
    assert position_for_user("other", created["id"]) is None

    closed = close_position(
        "owner",
        created["id"],
        exit_price=3.0,
        exit_at=exited_at,
    )

    assert closed is not None
    assert closed["status"] == "closed"
    assert closed["exit_at"] == exited_at
    assert closed["return_pct"] == 50.0
    assert close_position(
        "owner", created["id"], exit_price=3.1, exit_at=current.isoformat()
    ) is None


def test_trade_pages_use_ranked_alpha_and_pulse_radar() -> None:
    root = Path(__file__).parents[1]
    ticker = (root / "web/templates/ticker.html").read_text()
    alpha = (root / "web/templates/community.html").read_text()
    navigation = (root / "web/templates/mobile_base.html").read_text()
    app_source = (root / "src/runner_web/main.py").read_text()
    radar = (root / "web/templates/radar.html").read_text()

    assert "My trades" in ticker
    assert "Entry price" in ticker
    assert "Entry time" in ticker
    assert "Add exit" in ticker
    assert "🐺" in alpha
    assert "ranked" in alpha
    assert "data-alpha-reaction=\"bull\"" in alpha
    assert "data-alpha-reaction=\"bear\"" in alpha
    assert "comment_count" in alpha
    assert "alpha-heart" not in alpha
    assert "heartButton" not in ticker
    assert '<span class="tab-icon alpha-icon" aria-hidden="true">🐺</span>' in navigation
    assert '@app.post("/api/heart/{ticker}")' not in app_source
    assert "My Radar" not in radar
    assert "events from Pulse" in radar
