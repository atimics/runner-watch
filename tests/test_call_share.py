from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch
from starlette.testclient import TestClient

from runner_web import db, share_cards
from runner_web import main as web_main
from runner_web.caller_ids import ensure_machine_trader
from runner_web.calls import create_call
from runner_web.db import connection, init_db

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _database(tmp_path: Path, monkeypatch: MonkeyPatch, name: str) -> datetime:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / name)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    web_main.RATE_LIMITS.clear()
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    init_db()
    current = datetime.now(UTC)
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("alice", "alice", "Alice", "active", current.isoformat()),
        )
        ensure_machine_trader(database)
    return current


def _make_call(current: datetime, *, ticker: str = "OPK") -> dict:
    return create_call("alice", ticker, entry_price=12.5, entry_at=current.isoformat())


def test_call_share_names_the_caller_and_subject() -> None:
    share = share_cards.call_share(
        {
            "public_id": "abc123",
            "ticker": "OPK",
            "caller_handle": "sharpe",
            "entry_price": 12.5,
            "mark_price": 14.0,
            "return_pct": 12.0,
            "status": "active",
            "updated_at": "2026-08-24T14:00:00+00:00",
        }
    )

    assert share["title"] == "sharpe just called $OPK"
    assert "Entry $12.5" in share["summary"]
    assert "+12.0% so far" in share["summary"]
    assert share["path"] == "/c/abc123"
    assert share["card_path"].startswith("/c/abc123/card.png?v=")


def test_call_share_reports_a_settled_result() -> None:
    share = share_cards.call_share(
        {
            "public_id": "abc123",
            "ticker": "OPK",
            "caller_handle": "sharpe",
            "entry_price": 12.5,
            "mark_price": 10.0,
            "return_pct": -20.0,
            "status": "closed",
            "updated_at": "2026-08-24T20:00:00+00:00",
        }
    )

    assert share["title"] == "sharpe's $OPK Call closed -20.0%"
    assert "settled" in share["summary"]


def test_call_page_advertises_its_card(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch, "call-share-page.db")
    call = _make_call(current)

    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    try:
        response = client.get(f"/c/{call['public_id']}")
        missing = client.get("/c/does-not-exist")
    finally:
        client.close()

    assert response.status_code == 200
    html = response.text
    assert 'property="og:image"' in html
    assert f"/c/{call['public_id']}/card.png?v=" in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert '<meta name="description"' in html
    assert missing.status_code == 404


def test_call_card_route_serves_a_png(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch, "call-card.db")
    call = _make_call(current)

    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    try:
        card = client.get(f"/c/{call['public_id']}/card.png")
        missing = client.get("/c/does-not-exist/card.png")
    finally:
        client.close()

    assert card.status_code == 200
    assert card.headers["content-type"] == "image/png"
    assert card.content[:8] == PNG_SIGNATURE
    assert "public" in card.headers["cache-control"]
    assert missing.status_code == 404


def test_market_screen_shares_stock_calls_from_the_record() -> None:
    root = Path(__file__).parents[1]
    source = (root / "src/runner_web/main.py").read_text()
    template = (root / "web/templates/market_screen.html").read_text()

    assert '@app.get("/c/{public_id}", response_class=HTMLResponse)' in source
    assert '@app.get("/c/{public_id}/card.png")' in source
    assert "def call_share(" in (root / "src/runner_web/share_cards.py").read_text()
    assert 'href="/c/{{ screen.call.public_id }}"' in template
