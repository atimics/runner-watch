from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch
from starlette.requests import Request
from starlette.testclient import TestClient

from runner_web import db
from runner_web import main as web_main
from runner_web.db import init_db
from tests.test_mobile import insert_scan_run, insert_scored_snapshot

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _ticker_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 4210),
            "scheme": "http",
            "server": ("testserver", 80),
            "root_path": "",
        }
    )


def _ticker_detail(
    *,
    ticker: str = "ONE",
    price: Any = 12.5,
    change_pct: Any = 18.4,
    company: str = "One Corp",
    summary: str = "3 of 4 checks met",
    signals: list[str] | None = None,
    quote_time: str = "2026-08-24T14:00:00+00:00",
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "company": company,
        "current": {
            "price": price,
            "change_pct": change_pct,
            "signals": signals or [],
            "quote_time": quote_time,
        },
        "evidence_gate": {"summary": summary},
    }


def test_ticker_share_names_the_symbol_and_the_move() -> None:
    share = web_main.ticker_share(_ticker_detail())

    assert share["title"] == "$ONE · $12.5 · +18.4%"
    assert share["summary"] == "One Corp · 3 of 4 checks met"
    assert share["path"] == "/t/ONE"
    assert share["card_path"].startswith("/t/ONE/card.png?v=")


def test_ticker_share_version_tracks_the_latest_quote() -> None:
    first = web_main.ticker_share(_ticker_detail())
    repriced = web_main.ticker_share(_ticker_detail(price=13.25))
    requoted = web_main.ticker_share(
        _ticker_detail(quote_time="2026-08-24T14:05:00+00:00")
    )

    assert first["card_path"] != repriced["card_path"]
    assert first["card_path"] != requoted["card_path"]


def test_ticker_share_falls_back_when_the_quote_is_missing() -> None:
    share = web_main.ticker_share(
        _ticker_detail(price=None, change_pct=None, company="", summary="")
    )

    assert share["title"] == "$ONE"
    assert share["summary"] == "Scanner coverage and source evidence."


def test_ticker_share_uses_scanner_signals_when_there_is_no_summary() -> None:
    share = web_main.ticker_share(
        _ticker_detail(
            company="",
            summary="",
            signals=["Volume acceleration", "New high", "Ignored third"],
        )
    )

    assert share["summary"] == "Volume acceleration · New high"


def test_ticker_card_route_serves_a_cached_png(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ticker-card.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("ticker-card-run", captured_at, 1)
    insert_scored_snapshot(
        "ticker-card-snapshot", "ticker-card-run", "ONE", 70, 1, captured_at, price=3.5
    )

    client = TestClient(web_main.app, base_url=web_main.APP_ORIGIN)
    try:
        card = client.get("/t/ONE/card.png")
        missing = client.get("/t/NO-SUCH-TICKER/card.png")
    finally:
        client.close()

    assert card.status_code == 200
    assert card.headers["content-type"] == "image/png"
    assert card.content[:8] == PNG_SIGNATURE
    assert "public" in card.headers["cache-control"]
    assert missing.status_code == 404


def test_ticker_page_advertises_its_card(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ticker-share-page.db")
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("ticker-share-run", captured_at, 1)
    insert_scored_snapshot(
        "ticker-share-snapshot", "ticker-share-run", "ONE", 42, 1, captured_at
    )

    response = web_main.ticker_page("ONE", _ticker_request("/t/ONE"), None)
    html = response.body.decode()

    assert response.status_code == 200
    assert 'property="og:image"' in html
    assert "/t/ONE/card.png?v=" in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert '<meta name="description"' in html


def test_ticker_card_route_and_meta_are_public() -> None:
    root = Path(__file__).parents[1]
    source = (root / "src/runner_web/main.py").read_text()
    template = (root / "web/templates/ticker.html").read_text()

    assert '@app.get("/t/{ticker}/card.png")' in source
    assert "def ticker_share(" in source
    for tag in ("og:title", "og:description", "og:image", "twitter:card"):
        assert tag in template
    assert "block page_description" in template
