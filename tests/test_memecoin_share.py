from __future__ import annotations

import io
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from runner_web import db, share_cards
from runner_web import main as web_main

CA = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
COIN_ID = "chain-" + "a" * 64
PAGE_PATH = f"/memecoins/coin/{COIN_ID}"
DETAIL = {
    "coin": {
        "id": COIN_ID,
        "token_address": CA,
        "symbol": "7GCihg…W2hr",
        "claimed_name": "Bitcoin",
        "price": 0.0123,
        "price_label": "$0.0123",
        "change_24h": 42.5,
        "volume_label": "$1.2M",
        "market_cap_label": "$9.8M",
        "network": "solana",
        "observed_at": "2026-09-25T14:00:00+00:00",
        "findings": [{"title": "Top holder owns 30%"}],
    },
    "history": [
        {"observed_at": "2026-09-25T13:00:00+00:00", "price": 0.01},
        {"observed_at": "2026-09-25T14:00:00+00:00", "price": 0.0123},
    ],
    "calls": [],
}


class PreviewMetadata(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.meta = {}
        self.canonical = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.meta[attrs.get("property") or attrs.get("name")] = attrs.get("content")
        elif tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href")


@pytest.fixture
def preview_client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "preview.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    monkeypatch.setattr(web_main, "_memecoin_detail_payload", lambda _: dict(DETAIL))
    monkeypatch.setattr(web_main, "_cached_memecoin_detail", lambda _: dict(DETAIL))
    monkeypatch.setattr(web_main, "enforce_rate", lambda *a, **kw: None)
    monkeypatch.setattr(web_main, "current_user", lambda *_: None)
    client = TestClient(
        web_main.app,
        base_url=web_main.RUNNERS_ORIGIN,
        headers={"User-Agent": "TelegramBot (like TwitterBot)"},
    )
    yield client
    client.close()


def test_telegram_can_follow_the_page_metadata_to_a_large_image(preview_client):
    response = preview_client.get(PAGE_PATH + "?view=radar&q=coin")
    assert response.status_code == 200
    metadata = PreviewMetadata(response.text)
    meta = metadata.meta
    assert meta["og:type"] == "website"
    assert meta["og:url"] == web_main.RUNNERS_ORIGIN + PAGE_PATH
    assert metadata.canonical == meta["og:url"]
    assert meta["og:site_name"] == "RATi"
    assert meta["og:title"].startswith("7GCihg")
    assert CA in meta["og:description"]
    assert meta["description"] == meta["og:description"]
    assert meta["twitter:title"] == meta["og:title"]
    assert meta["twitter:description"] == meta["og:description"]
    assert meta["twitter:card"] == "summary_large_image"
    assert meta["twitter:image"] == meta["og:image"]
    assert meta["twitter:image:alt"] == meta["og:image:alt"]
    assert meta["og:image"].startswith(web_main.RUNNERS_ORIGIN + PAGE_PATH + "/card.png?v=")
    assert meta["og:image:type"] == "image/png"
    card = preview_client.get(meta["og:image"])
    assert card.status_code == 200
    assert card.headers["content-type"] == meta["og:image:type"]
    with Image.open(io.BytesIO(card.content)) as image:
        assert image.size == (int(meta["og:image:width"]), int(meta["og:image:height"]))
        assert image.size == (1200, 630)


@pytest.mark.parametrize("suffix", ["", "/card.png"])
def test_preview_head_requests_match_get_headers(preview_client, suffix):
    url = PAGE_PATH + suffix
    page = preview_client.get(url)
    head = preview_client.head(url)
    assert head.status_code == page.status_code == 200
    assert head.content == b""
    for header in ("content-type", "content-length", "cache-control"):
        assert head.headers.get(header) == page.headers.get(header)


@pytest.mark.parametrize("method", ["get", "head"])
def test_missing_coin_preview_returns_404(preview_client, monkeypatch, method):
    from fastapi import HTTPException

    def missing(_):
        raise HTTPException(404, "Coin not found")

    monkeypatch.setattr(web_main, "_memecoin_detail_payload", missing)
    monkeypatch.setattr(web_main, "_cached_memecoin_detail", lambda _: None)
    for suffix in ("", "/card.png"):
        assert getattr(preview_client, method)(PAGE_PATH + suffix).status_code == 404


def test_a_shared_coin_unfurls_under_its_address_not_its_claimed_name():
    share = share_cards.memecoin_share(DETAIL, "chain-abc")

    assert share["title"].startswith("7GCihg")
    assert "+42.5% 24h" in share["title"]
    assert CA in share["summary"]
    assert "Bitcoin" not in share["title"] + share["summary"]
    assert share["card_path"].startswith("/memecoins/coin/chain-abc/card.png?v=")


def test_the_coin_card_is_a_share_sized_png():
    image = Image.open(io.BytesIO(share_cards._memecoin_card_png(DETAIL)))

    assert image.size == (1200, 630)


def test_the_coin_card_route_serves_a_png(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(web_main, "_cached_memecoin_detail", lambda coin_id: dict(DETAIL))
    monkeypatch.setattr(web_main, "enforce_rate", lambda *a, **kw: None)
    client = TestClient(web_main.app)
    card = client.get("/memecoins/coin/chain-abc/card.png")
    client.close()

    assert card.status_code == 200
    assert card.headers["content-type"] == "image/png"


def test_an_unreported_amount_is_left_off_the_share():
    detail = {**DETAIL, "coin": {**DETAIL["coin"], "market_cap_label": "—"}}

    share = share_cards.memecoin_share(detail, "chain-abc")

    assert "Market cap" not in share["summary"]
    assert "Volume $1.2M" in share["summary"]
