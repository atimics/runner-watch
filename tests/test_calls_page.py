from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from runner_web import db
from runner_web import main as web_main
from runner_web.caller_ids import ensure_machine_trader
from runner_web.calls import create_call
from runner_web.db import connection, init_db
from runner_web.flash_evaluations import flash_open_calls
from tests.test_flash_evaluations import _record


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
        database.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at,authenticated_at) "
            "VALUES(?,?,?,?,?)",
            (
                web_main.token_hash("alice-session"),
                "alice",
                current.isoformat(),
                (current + timedelta(days=1)).isoformat(),
                current.isoformat(),
            ),
        )
        ensure_machine_trader(database)
    return current


def test_flash_open_calls_orders_by_confidence_and_dedupes(tmp_path, monkeypatch) -> None:
    _database(tmp_path, monkeypatch, "open-calls.db")
    start = "2026-08-24T19:00:00+00:00"
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("flash-user", "flash_user", "Flash user", "active", start),
        )
    _record("one", "AAA", "up", 0.9, start_at=start)
    _record("two", "BBB", "up", 0.8, start_at=start)
    _record("three", "AAA", "down", 0.35, start_at=start)
    _record("four", "CCC", "down", 0.45, start_at=start)

    calls = flash_open_calls()["calls"]

    assert [call["ticker"] for call in calls] == ["AAA", "BBB", "CCC"]
    assert calls[0]["direction"] == "up"
    assert calls[0]["confidence"] == 0.9
    assert calls[2]["direction"] == "down"
    assert calls[2]["confidence"] == 0.55
    assert all(call["version_label"] for call in calls)


def test_calls_page_renders_flash_and_signed_out_cta(tmp_path, monkeypatch) -> None:
    _database(tmp_path, monkeypatch, "calls-page.db")
    from fastapi.testclient import TestClient

    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)

    response = client.get("/calls")

    assert response.status_code == 200
    html = response.text
    assert "<h1>Calls</h1>" in html
    assert "/static/market-screen.css" in html
    assert "Create your passkey" in html
    assert 'id="my-calls-heading"' not in html
    assert "No open directional calls" in html


def test_calls_page_signed_in_lists_my_calls_and_flash_picks(tmp_path, monkeypatch) -> None:
    current = _database(tmp_path, monkeypatch, "calls-page-signed-in.db")
    created = create_call("alice", "OPK", entry_price=12.5, entry_at=current.isoformat())
    assert created is not None
    start = "2026-08-24T19:00:00+00:00"
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("flash-user", "flash_user", "Flash user", "active", start),
        )
    _record("five", "OPK", "up", 0.88, start_at=start)

    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    client.cookies.set(web_main.SESSION_COOKIE, "alice-session")

    response = client.get("/calls")

    assert response.status_code == 200
    html = response.text
    assert 'id="my-calls-heading"' in html
    assert "You vs Flash" in html
    assert 'aria-label="Your hit rate"' in html
    assert 'aria-label="Flash hit rate"' in html
    assert "$OPK" in html
    assert "Full public record" in html
    assert "No open directional calls" not in html
    assert "88%" in html


def test_calls_comparison_uses_visible_graded_records() -> None:
    comparison = web_main._calls_comparison(
        {
            "stats": {
                "wins": 3,
                "losses": 1,
                "settled": 5,
                "open": 2,
            }
        },
        {
            "headline_rate_visible": True,
            "hit_rate": 0.6,
            "hits": 12,
            "misses": 8,
            "settled": 20,
            "pending": 3,
        },
    )

    assert comparison == {
        "you": {
            "hit_rate_pct": 75,
            "hits": 3,
            "misses": 1,
            "graded": 4,
            "settled": 5,
            "open": 2,
        },
        "flash": {
            "hit_rate_pct": 60,
            "hits": 12,
            "misses": 8,
            "settled": 20,
            "pending": 3,
        },
        "leader": "you",
        "gap_points": 15,
    }


def test_calls_comparison_keeps_flash_rate_gated() -> None:
    comparison = web_main._calls_comparison(
        {"stats": {"wins": 1, "losses": 1, "settled": 2, "open": 0}},
        {
            "headline_rate_visible": False,
            "hit_rate": 0.8,
            "hits": 4,
            "misses": 1,
            "settled": 5,
            "pending": 2,
        },
    )

    assert comparison["you"]["hit_rate_pct"] == 50
    assert comparison["flash"]["hit_rate_pct"] is None
    assert comparison["leader"] is None
    assert comparison["gap_points"] is None


def test_calls_page_renders_direct_hit_rate_comparison(tmp_path, monkeypatch) -> None:
    _database(tmp_path, monkeypatch, "calls-comparison.db")
    monkeypatch.setattr(
        web_main,
        "_calls_page_data",
        lambda _session: {
            "mine": {
                "handle": "bullish-alpaca",
                "calls": [],
                "stats": {"wins": 3, "losses": 1, "settled": 5, "open": 2},
            },
            "comparison": web_main._calls_comparison(
                {
                    "stats": {
                        "wins": 3,
                        "losses": 1,
                        "settled": 5,
                        "open": 2,
                    }
                },
                {
                    "headline_rate_visible": True,
                    "hit_rate": 0.6,
                    "hits": 12,
                    "misses": 8,
                    "settled": 20,
                    "pending": 3,
                },
            ),
            "record": {"headline_rate_visible": True, "hit_rate": 0.6},
            "stock_picks": [],
            "sports_picks": [],
            "memecoin_picks": [],
        },
    )
    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    client.cookies.set(web_main.SESSION_COOKIE, "alice-session")

    response = client.get("/calls")

    assert response.status_code == 200
    assert "You lead by 15 points" in response.text
    assert 'aria-label="Your hit rate">75%' in response.text
    assert 'aria-label="Flash hit rate">60%' in response.text
    assert "3 hits · 1 miss" in response.text
    assert "12 hits · 8 misses" in response.text
    assert "All markets" in response.text
    assert "Next-close stocks" in response.text


def test_my_calls_redirects_signed_in_users_to_calls(tmp_path, monkeypatch) -> None:
    _database(tmp_path, monkeypatch, "calls-redirect.db")
    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    client.cookies.set(web_main.SESSION_COOKIE, "alice-session")

    response = client.get("/my-calls", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/calls"
