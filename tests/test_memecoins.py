import asyncio
import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from runner_web import db, memecoin_store, memecoins
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
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
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


def test_detail_has_its_quote_receipt_and_optional_market_fields(market_db):
    result = seed(
        [
            coin(
                high_24h=0.13,
                low_24h=0.11,
                fully_diluted_valuation=20_000_000_000,
                circulating_supply=0,
                total_supply=150_000_000_000,
                max_supply=None,
            )
        ]
    )

    detail = memecoins.memecoin_detail("dogecoin", at=AT)

    assert detail is not None
    assert detail["status"] == "ok"
    assert detail["in_current_snapshot"] is True
    assert detail["coin"]["detail_url"] == "/memecoins/dogecoin"
    assert detail["coin"]["price_label"] == "$0.12"
    assert detail["coin"]["volume_label"] == "$900.00M"
    assert detail["coin"]["market_cap_label"] == "$18.00B"
    assert detail["coin"]["high_24h"] == 0.13
    assert detail["coin"]["low_24h"] == 0.11
    assert detail["coin"]["fully_diluted_valuation"] == 20_000_000_000
    assert detail["coin"]["circulating_supply"] == 0
    assert detail["coin"]["total_supply"] == 150_000_000_000
    assert detail["coin"]["max_supply"] is None
    assert detail["evidence"] == {
        "source_url": "https://www.coingecko.com/en/coins/dogecoin",
        "run_id": result["run_id"],
        "observed_at": AT.isoformat(),
        "collected_at": AT.isoformat(),
        "checks": {
            "source_time_known": True,
            "quote_fresh": True,
            "volume_known": True,
            "market_cap_known": True,
        },
    }
    assert detail["history"] == [
        {"observed_at": AT.isoformat(), "collected_at": AT.isoformat(), "price": 0.12}
    ]
    assert memecoins.memecoin_market(at=AT)["rows"][0]["detail_url"] == "/memecoins/dogecoin"


def test_detail_keeps_coin_identity_separate_from_symbols(market_db):
    seed([coin("first", current_price=0.1), coin("second", current_price=0.2)])

    first = memecoins.memecoin_detail("first", at=AT)
    second = memecoins.memecoin_detail("second", at=AT)

    assert first["coin"]["symbol"] == second["coin"]["symbol"] == "DOGE"
    assert first["coin"]["price"] == first["history"][0]["price"] == 0.1
    assert second["coin"]["price"] == second["history"][0]["price"] == 0.2
    assert memecoins.memecoin_detail("DOGE", at=AT) is None
    assert memecoins.memecoin_detail("unknown", at=AT) is None
    assert memecoins.memecoin_detail("../first", at=AT) is None


def test_coin_that_leaves_snapshot_keeps_its_own_receipt_and_freshness(market_db):
    original = seed()
    next_at = AT + timedelta(minutes=5)
    latest = seed([coin("pepe", last_updated=next_at.isoformat())], at=next_at)

    detail = memecoins.memecoin_detail("dogecoin", at=AT + timedelta(minutes=6))

    assert [row["id"] for row in memecoins.memecoin_market(at=next_at)["rows"]] == ["pepe"]
    assert detail["in_current_snapshot"] is False
    assert detail["status"] == "ok"
    assert detail["coin"]["stale"] is False
    assert detail["collected_at"] == AT.isoformat()
    assert detail["evidence"]["run_id"] == original["run_id"]
    assert detail["evidence"]["run_id"] != latest["run_id"]
    stale = memecoins.memecoin_detail("dogecoin", at=AT + timedelta(minutes=16))
    assert stale["status"] == "stale"
    assert stale["coin"]["stale"] is True


def test_history_keeps_real_observations_in_time_order(market_db):
    seed()
    seed(at=AT + timedelta(minutes=5))
    new_at = AT + timedelta(minutes=10)
    seed([coin(current_price=0.14, last_updated=new_at.isoformat())], at=new_at)

    detail = memecoins.memecoin_detail("dogecoin", at=new_at)

    assert [point["observed_at"] for point in detail["history"]] == [
        AT.isoformat(),
        new_at.isoformat(),
    ]
    assert [point["price"] for point in detail["history"]] == [0.12, 0.14]
    assert detail["history"][0]["collected_at"] == (AT + timedelta(minutes=5)).isoformat()


@pytest.mark.parametrize("observed_at", [None, "2026-09-05T07:00:00", "2026-09-05T08:00:00Z"])
def test_history_requires_a_plausible_source_time(market_db, observed_at):
    seed([coin(last_updated=observed_at)])

    detail = memecoins.memecoin_detail("dogecoin", at=AT)

    assert detail["status"] == "stale"
    assert detail["coin"]["stale"] is True
    assert detail["history"] == []


def test_history_storage_and_detail_query_are_bounded(market_db, monkeypatch):
    monkeypatch.setattr(memecoin_store, "MAX_HISTORY_POINTS", 3)
    monkeypatch.setattr(memecoin_store, "MAX_DETAIL_HISTORY", 2)
    for index in range(4):
        collected = AT + timedelta(minutes=index * 5)
        seed([coin(current_price=1 + index, last_updated=collected.isoformat())], at=collected)

    detail = memecoins.memecoin_detail("dogecoin", at=collected, history_limit=10_000)

    assert [point["price"] for point in detail["history"]] == [3, 4]
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM memecoin_quote_history").fetchone()[0] == 3
    later = AT + timedelta(days=8)
    seed([coin("pepe", last_updated=later.isoformat())], at=later)
    retired = memecoins.memecoin_detail("dogecoin", at=later)
    assert retired["coin"]["price"] == 4
    assert retired["status"] == "stale"
    assert retired["history"] == []
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM memecoin_quote_history").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM memecoin_assets").fetchone()[0] == 2


def test_failed_refresh_keeps_detail_receipt_and_disabled_feed_marks_it_stale(
    market_db, monkeypatch
):
    original = seed()
    seed([], at=AT + timedelta(minutes=5))

    detail = memecoins.memecoin_detail("dogecoin", at=AT + timedelta(minutes=6))

    assert detail["refresh_failed"] is True
    assert detail["coin"]["price"] == 0.12
    assert detail["evidence"]["run_id"] == original["run_id"]
    monkeypatch.setenv("MEMECOINS_ENABLED", "false")
    disabled = memecoins.memecoin_detail("dogecoin", at=AT + timedelta(minutes=6))
    assert disabled["status"] == "disabled"
    assert disabled["coin"]["stale"] is True
    assert disabled["evidence"]["checks"]["quote_fresh"] is False


def test_detail_reads_existing_snapshot_before_first_collection_after_upgrade(market_db):
    rows = memecoins.normalize_memecoins([coin()])
    with connection() as database:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
            (
                "memecoins_snapshot",
                json.dumps({"rows": rows, "collected_at": AT.isoformat(), "run_id": "old-run"}),
                AT.isoformat(),
            ),
        )

    detail = memecoins.memecoin_detail("dogecoin", at=AT)

    assert detail["status"] == "ok"
    assert detail["evidence"]["run_id"] == "old-run"
    assert detail["history"] == []


def test_upgrade_keeps_legacy_coin_after_the_first_new_snapshot(market_db, monkeypatch):
    with connection() as database:
        database.execute("DROP TABLE memecoin_quote_history")
        database.execute("DROP TABLE memecoin_assets")
        database.execute("DELETE FROM schema_migrations WHERE version=55")
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
            (
                "memecoins_snapshot",
                json.dumps(
                    {
                        "rows": memecoins.normalize_memecoins([coin()]),
                        "collected_at": AT.isoformat(),
                        "run_id": "legacy-run",
                    }
                ),
                AT.isoformat(),
            ),
        )

    init_db()
    later = AT + timedelta(minutes=5)
    seed([coin("pepe", last_updated=later.isoformat())], at=later)
    detail = memecoins.memecoin_detail("dogecoin", at=later)

    assert detail["coin"]["price"] == 0.12
    assert detail["evidence"]["run_id"] == "legacy-run"
    assert detail["in_current_snapshot"] is False
    assert detail["history"] == []


def test_optional_market_fields_reject_invalid_values():
    row = memecoins.normalize_memecoins([coin(high_24h=float("inf"), low_24h=-1, max_supply=True)])[
        0
    ]
    assert row["high_24h"] is row["low_24h"] is row["max_supply"] is None
    assert row["fully_diluted_valuation"] is None


def test_sub_dollar_price_rounds_to_a_complete_label():
    assert memecoins._price_label(0.99999) == "$1"
    assert memecoins._price_label(0.00001234) == "$0.00001234"
