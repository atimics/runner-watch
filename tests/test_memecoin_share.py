from __future__ import annotations

import io

from PIL import Image

from runner_web import main as web_main

CA = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
DETAIL = {
    "coin": {
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
}


def test_a_shared_coin_unfurls_under_its_address_not_its_claimed_name():
    share = web_main.memecoin_share(DETAIL, "chain-abc")

    assert share["title"].startswith("7GCihg")
    assert "+42.5% 24h" in share["title"]
    assert CA in share["summary"]
    assert "Bitcoin" not in share["title"] + share["summary"]
    assert share["card_path"].startswith("/memecoins/coin/chain-abc/card.png?v=")


def test_the_coin_card_is_a_share_sized_png():
    image = Image.open(io.BytesIO(web_main._memecoin_card_png(DETAIL)))

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

    share = web_main.memecoin_share(detail, "chain-abc")

    assert "Market cap" not in share["summary"]
    assert "Volume $1.2M" in share["summary"]
