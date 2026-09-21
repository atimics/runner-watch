from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
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
    assert share["path"] == "/stock/ONE"
    assert share["card_path"].startswith("/stock/ONE/card.png?v=")


def test_ticker_share_version_tracks_the_latest_quote() -> None:
    first = web_main.ticker_share(_ticker_detail())
    repriced = web_main.ticker_share(_ticker_detail(price=13.25))
    requoted = web_main.ticker_share(_ticker_detail(quote_time="2026-08-24T14:05:00+00:00"))

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
        card = client.get("/stock/ONE/card.png")
        missing = client.get("/stock/NO-SUCH-TICKER/card.png")
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
    insert_scored_snapshot("ticker-share-snapshot", "ticker-share-run", "ONE", 42, 1, captured_at)

    response = web_main.ticker_page("ONE", _ticker_request("/stock/ONE"), None)
    html = response.body.decode()

    assert response.status_code == 200
    assert 'property="og:image"' in html
    assert "/stock/ONE/card.png?v=" in html
    assert 'name="twitter:card" content="summary_large_image"' in html
    assert '<meta name="description"' in html


def test_ticker_page_includes_the_robinhood_chain_token(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ticker-rh-chain.db")
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_scan_run("ticker-rh-run", captured_at, 1)
    insert_scored_snapshot("ticker-rh-snapshot", "ticker-rh-run", "ONE", 42, 1, captured_at)
    token = {
        "symbol": "ONE",
        "name": "One Corp • Robinhood Token",
        "contract_address": "0x1Cdad396DB64BDa184d5182A97Dd9B3C62100b7D",
        "chain_id": 4663,
        "multiplier": "1.000000000000000000",
        "status": "active",
        "docs_url": "https://docs.robinhood.com/chain/stock-tokens",
    }
    monkeypatch.setattr(web_main, "stock_token", lambda ticker: token)

    response = web_main.ticker_page("ONE", _ticker_request("/stock/ONE"), None)

    assert response.status_code == 200
    html = response.body.decode()
    assert "Robinhood Chain token" in html
    assert token["contract_address"] in html
    assert "not the underlying stock" in html



def test_ticker_card_route_and_meta_are_public() -> None:
    root = Path(__file__).parents[1]
    source = (root / "src/runner_web/main.py").read_text()
    # The share meta lives on the shell the live ticker page extends.
    template = (root / "web/templates/market_screen.html").read_text()

    assert '@app.get("/t/{ticker}/card.png")' in source
    assert "def ticker_share(" in source
    for tag in ("og:title", "og:description", "og:image", "twitter:card"):
        assert tag in template


def _colors(image) -> set[tuple[int, int, int]]:
    return {color for _count, color in image.getcolors(maxcolors=1_000_000)}


def test_the_state_badge_keeps_off_the_daily_change_line() -> None:
    """The badge used to sit on top of the \"Daily change · …\" line."""
    import io

    from PIL import Image

    detail = _ticker_detail()
    detail["current"]["trade_state"] = "WATCH"
    png = web_main._ticker_card_png(detail)

    image = Image.open(io.BytesIO(png)).convert("RGB")
    assert image.size == (1200, 630)
    date_band = image.crop((640, web_main.CARD_DATE_Y, 1145, web_main.CARD_DATE_Y + 24))
    badge_fill = (0x12, 0x30, 0x21)
    assert badge_fill not in _colors(date_band)
    # It does still render, on the change row beside the move.
    change_band = image.crop((640, web_main.CARD_CHANGE_Y, 1145, web_main.CARD_CHANGE_Y + 30))
    assert badge_fill in _colors(change_band)


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ("2026-09-12T15:50:00+00:00", "Sep 12, 2026 · 11:50 EDT"),
        ("2026-01-15T14:30:00Z", "Jan 15, 2026 · 09:30 EST"),
    ],
)
def test_card_times_are_eastern(stamp, expected):
    assert web_main._card_time(stamp) == expected


def test_the_quote_time_moved_to_the_card_footer_without_the_prefix() -> None:
    import io

    from PIL import Image

    png = web_main._ticker_card_png(_ticker_detail(quote_time="2026-09-12T15:50:00+00:00"))
    image = Image.open(io.BytesIO(png)).convert("RGB")
    muted = (0x65, 0x71, 0x6B)

    footer = image.crop((700, web_main.CARD_FOOTER_Y, 1145, web_main.CARD_FOOTER_Y + 28))
    assert muted in _colors(footer)
    # Nothing is written in the price block any more.
    price_rows = image.crop((700, web_main.CARD_DATE_Y, 1145, web_main.CARD_DATE_Y + 24))
    assert muted not in _colors(price_rows)


def test_the_company_name_sits_under_the_ticker_instead_of_over_the_price() -> None:
    import io

    from PIL import Image

    detail = _ticker_detail(company="Silvia, Inc.")
    png = web_main._ticker_card_png(detail)
    image = Image.open(io.BytesIO(png)).convert("RGB")
    company_ink = (0x7E, 0x8B, 0x86)

    under_ticker = image.crop(
        (95, web_main.CARD_COMPANY_Y, 700, web_main.CARD_COMPANY_Y + 30)
    )
    over_price = image.crop((700, 70, 1105, 120))
    assert company_ink in _colors(under_ticker)
    assert company_ink not in _colors(over_price)
    # It stays below the symbol, clear of the change and date rows.
    assert web_main.CARD_COMPANY_Y + 30 <= web_main.CARD_DATE_Y


def test_the_badge_sits_left_of_the_move_and_inside_the_card() -> None:
    detail = _ticker_detail()
    detail["current"]["trade_state"] = "AVOID"
    detail["current"]["rug_level"] = "high"

    class Recorder:
        def __init__(self) -> None:
            self.boxes: list[tuple[int, int, int, int]] = []

        def textlength(self, *_args, **_kwargs):
            return 60

        def rounded_rectangle(self, box, **_kwargs):
            self.boxes.append(box)

        def text(self, *_args, **_kwargs):
            return None

    recorder = Recorder()
    box = web_main._draw_ticker_badge(
        recorder, detail["current"], right=1000, top=web_main.CARD_CHANGE_Y
    )

    assert box is not None
    left, top, right, bottom = box
    assert right == 1000
    assert left < right
    assert top == web_main.CARD_CHANGE_Y
    assert bottom <= web_main.CARD_DATE_Y
    assert recorder.boxes == [box]
