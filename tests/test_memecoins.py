import asyncio
import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from runner_web import db, memecoins
from runner_web.db import connection, init_db

AT = datetime(2026, 9, 5, 7, tzinfo=UTC)


def coin(coin_id="dogecoin", **extra):
    return {
        "id": coin_id,
        "symbol": "doge",
        "name": "Dogecoin",
        "current_price": 0.12,
        "market_cap": 18_000_000_000,
        "total_volume": 900_000_000,
        "price_change_percentage_24h": 2.5,
        "last_updated": AT.isoformat(),
        **extra,
    }


@pytest.fixture
def market_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "memecoins.db")
    monkeypatch.setenv("MEMECOINS_ENABLED", "true")
    init_db()


def seed(rows=None, at=AT):
    body = json.dumps(rows if rows is not None else [coin()]).encode()
    return memecoins.refresh_memecoins(download=lambda *_: body, at=at)


def test_normalization_uses_coin_ids_and_keeps_tiny_prices():
    rows = memecoins.normalize_memecoins(
        [
            coin("first", current_price=0.0000000000042),
            coin("second"),
            coin("first", current_price=10),
            coin("../bad"),
            coin("zero", current_price=0),
            coin("bool", current_price=True),
            coin("nan", current_price=float("nan")),
            None,
        ]
    )
    assert [row["id"] for row in rows] == ["first", "second"]
    assert rows[0]["price"] == 0.0000000000042
    assert rows[0]["source_url"] == "https://www.coingecko.com/en/coins/first"
    assert rows[0]["symbol"] == rows[1]["symbol"] == "DOGE"


def test_unknown_fields_keep_their_meaning():
    row = memecoins.normalize_memecoins(
        [
            coin(
                total_volume=-1,
                market_cap=float("inf"),
                price_change_percentage_24h=None,
                last_updated="2026-09-05T07:00:00",
            )
        ]
    )[0]
    assert row["volume_24h"] is row["market_cap"] is row["change_24h"] is None
    assert row["observed_at"] is None


@pytest.mark.parametrize(
    "payload", [{"error": "rate limit"}, [], [coin(current_price=-1)], [coin()] * 101]
)
def test_invalid_feed_is_rejected(payload):
    with pytest.raises(ValueError):
        memecoins.normalize_memecoins(payload)


def test_refresh_records_source_and_shares_request_budget(market_db):
    result = seed()
    assert result["status"] == "ok"
    assert (
        memecoins.refresh_memecoins(download=lambda *_: pytest.fail("duplicate fetch"), at=AT)[
            "status"
        ]
        == "cached"
    )
    assert seed(at=AT + timedelta(seconds=300))["status"] == "ok"
    with connection() as database:
        runs = database.execute("SELECT * FROM ingestion_runs WHERE source='coingecko'").fetchall()
        assert len(runs) == 2
        assert all(row["status"] == "success" and row["received_count"] == 1 for row in runs)
        assert database.execute("SELECT COUNT(*) FROM scan_snapshots").fetchone()[0] == 0
    market = memecoins.memecoin_market(at=AT + timedelta(seconds=301))
    assert market["rows"][0]["price_label"] == "$0.12"
    assert market["rows"][0]["volume_label"] == "$900.00M"
    assert market["status"] == "ok"


def test_source_failure_keeps_saved_snapshot_and_retries_on_cadence(market_db):
    seed()

    def fail(*_):
        raise HTTPError(memecoins.MARKETS_URL, 429, "Too many requests", {}, None)

    assert (
        memecoins.refresh_memecoins(download=fail, at=AT + timedelta(minutes=5))["status"]
        == "error"
    )
    market = memecoins.memecoin_market(at=AT + timedelta(minutes=6))
    assert market["rows"][0]["price"] == 0.12
    assert market["collected_at"] == AT.isoformat()
    assert market["refresh_failed"] is True
    assert memecoins.memecoin_market(at=AT + timedelta(minutes=16))["status"] == "stale"
    seed(at=AT + timedelta(minutes=20))
    assert memecoins.memecoin_market(at=AT + timedelta(minutes=20))["refresh_failed"] is False


def test_bad_and_empty_responses_preserve_last_success(market_db):
    seed()
    assert seed([], at=AT + timedelta(minutes=5))["status"] == "error"
    assert memecoins.memecoin_market(at=AT + timedelta(minutes=6))["rows"][0]["id"] == "dogecoin"


def test_sort_search_and_stale_times(market_db):
    seed(
        [
            coin(
                "shiba-inu",
                symbol="shib",
                name="Shiba Inu",
                current_price=0.00001234,
                total_volume=3,
                price_change_percentage_24h=-10,
            ),
            coin(
                "pepe", symbol="pepe", name="Pepe", total_volume=5, price_change_percentage_24h=20
            ),
            coin(
                "dogecoin", total_volume=None, price_change_percentage_24h=None, last_updated=None
            ),
            coin("future", total_volume=1, last_updated=(AT + timedelta(hours=1)).isoformat()),
        ]
    )
    market = memecoins.memecoin_market(at=AT)
    assert [row["id"] for row in market["rows"]] == ["pepe", "shiba-inu", "future", "dogecoin"]
    assert market["rows"][-1]["stale"] is True
    assert market["rows"][-2]["stale"] is True
    assert memecoins.memecoin_market(sort="losers", at=AT)["rows"][0]["id"] == "shiba-inu"
    assert memecoins.memecoin_market(sort="gainers", at=AT)["rows"][0]["id"] == "pepe"
    filtered = memecoins.memecoin_market(query=" SHIB ", at=AT)
    assert filtered["total"] == 4 and len(filtered["rows"]) == 1
    assert filtered["rows"][0]["price_label"] == "$0.00001234"
    assert memecoins.memecoin_market(sort="bad", at=AT)["sort"] == "volume"


def test_pending_disabled_and_failure_before_first_snapshot(market_db, monkeypatch):
    assert memecoins.memecoin_market(at=AT)["status"] == "pending"
    seed([])
    assert memecoins.memecoin_market(at=AT)["status"] == "unavailable"
    monkeypatch.setenv("MEMECOINS_ENABLED", "false")
    assert (
        memecoins.refresh_memecoins(download=lambda *_: pytest.fail("disabled fetch"))["status"]
        == "disabled"
    )
    assert memecoins.memecoin_market(at=AT)["status"] == "disabled"


def test_public_routes_render_prices_and_escape_provider_text(market_db, monkeypatch):
    from runner_web import main

    seed([coin(name='<script>alert("coin")</script>')], at=datetime.now(UTC))
    monkeypatch.setattr(main, "enforce_rate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "current_user", lambda *_: None)
    client = TestClient(main.app)
    try:
        response = client.get("/memecoins")
        assert response.status_code == 200
        assert "$0.12" in response.text
        assert '<script>alert("coin")</script>' not in response.text
        assert "&lt;script&gt;" in response.text
        assert 'href="/memecoins" aria-current="page"' in response.text
        data = client.get("/api/memecoins?q=doge&sort=market_cap").json()
        assert data["currency"] == "USD"
        assert data["rows"][0]["id"] == "dogecoin"
        assert data["source"] == "CoinGecko"
    finally:
        client.close()


def test_worker_refresh_runs_in_a_thread_and_cancels(monkeypatch):
    from runner_web import main

    calls = []

    async def run_refresh(function):
        calls.append(function)

    async def sleep(seconds):
        assert seconds == memecoins.REFRESH_SECONDS
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "run_in_threadpool", run_refresh)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.memecoin_worker())
    assert calls == [main.refresh_memecoins]


def test_download_uses_bounded_public_category_request(monkeypatch):
    import io

    seen = []

    def open_url(request, timeout):
        seen.append((request, timeout))
        return io.BytesIO(b"[]")

    monkeypatch.setattr(memecoins.urllib.request, "urlopen", open_url)
    assert memecoins._download(memecoins.MARKETS_URL, 10) == b"[]"
    params = parse_qs(urlparse(seen[0][0].full_url).query)
    assert params["category"] == ["meme-token"]
    assert params["vs_currency"] == ["usd"]
    assert params["per_page"] == ["100"]
