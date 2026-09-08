import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from runner_web import db, memecoins

AT = datetime(2026, 9, 8, 17, tzinfo=UTC)


@pytest.fixture
def market_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "chain.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setenv("MEMECOINS_ENABLED", "true")
    db.init_db()
    monkeypatch.setattr(
        memecoins,
        "discover_pools",
        lambda **_: {
            "pools": [
                {
                    "pool_address": "Pool123",
                    "token_address": "AbC123",
                    "created_at": (AT - timedelta(minutes=10)).isoformat(),
                }
            ],
            "partial": False,
        },
    )


def pool(token="AbC123", network="solana", address="Pool123", **attrs):
    return {
        "attributes": {
            "address": address,
            "base_token_price_usd": "0.0000000000042",
            "reserve_in_usd": "2000",
            "volume_usd": {"h24": "100"},
            "transactions": {"h24": {"buys": 3, "sells": 1}},
            "price_change_percentage": {"h24": "2"},
            "pool_created_at": (AT - timedelta(minutes=10)).isoformat(),
            **attrs,
        },
        "relationships": {
            "network": {"data": {"id": network}},
            "base_token": {"data": {"id": f"{network}_{token}"}},
        },
    }


def normalize(*pools):
    return memecoins.normalize_chain_pools({"data": list(pools)}, at=AT)


def test_discovery_is_independent_of_metadata_and_marketing():
    raw = pool()
    original = normalize(raw)
    raw["attributes"].update(name="promoted meme", symbol="AD", market_cap_usd="99999999")
    raw.update(boost=100, category="meme-token", description="Buy this", sentiment=100)
    assert normalize(raw) == original
    assert original[0]["token_address"] == "AbC123"
    assert original[0]["price"] == 0.0000000000042
    assert original[0]["market_cap"] is None
    assert original[0]["time_basis"] == "indexer_fetch"
    assert original[0]["observed_at"] == AT.isoformat()


def test_identity_preserves_chain_and_case_and_deduplicates_pools():
    rows = normalize(
        pool(),
        pool(token="abc123"),
        pool(network="eth"),
        pool(address="DeepPool", reserve_in_usd="4000"),
    )
    assert len({row["id"] for row in rows}) == 3
    selected = next(
        row for row in rows if row["network"] == "solana" and row["token_address"] == "AbC123"
    )
    assert selected["pool_address"] == "DeepPool"
    address = "0x" + "aB" * 20
    assert len(normalize(pool(token=address), pool(token=address.lower()))) == 1


@pytest.mark.parametrize(
    "attrs",
    [
        {"base_token_price_usd": True},
        {"base_token_price_usd": "nan"},
        {"reserve_in_usd": "999"},
        {"reserve_in_usd": None},
        {"volume_usd": {"h24": "0"}},
        {"transactions": {"h24": {"buys": 0, "sells": 0}}},
        {"pool_created_at": None},
        {"pool_created_at": (AT + timedelta(days=1)).isoformat()},
        {"address": "../evil"},
    ],
)
def test_invalid_and_inactive_pools_are_filtered(attrs):
    assert normalize(pool(**attrs)) == []


def test_malformed_records_and_payloads():
    assert normalize(None, {}, {"attributes": []}) == []
    for payload in ([], {"error": "rate limited"}, {"data": None}, {"data": [pool()] * 101}):
        with pytest.raises(ValueError):
            memecoins.normalize_chain_pools(payload, at=AT)
    broken = copy.deepcopy(pool())
    broken["relationships"]["base_token"]["data"]["id"] = "eth_AbC123"
    assert normalize(broken) == []


def test_real_worker_path_and_failure_receipts(market_db):  # noqa: F811
    requests = []

    def download(url, timeout):
        requests.append(url)
        return json.dumps({"data": [pool()]}).encode()

    assert memecoins.refresh_memecoins(download=download, at=AT)["count"] == 1
    assert requests == ["https://api.geckoterminal.com/api/v2/networks/solana/pools/multi/Pool123"]
    market = memecoins.memecoin_market(at=AT)
    assert market["source"] == "GeckoTerminal"
    row = market["rows"][0]
    detail = memecoins.memecoin_detail(row["id"], at=AT)
    assert detail["source"] == "GeckoTerminal"
    assert detail["history"][0]["price"] == row["price"]
    assert detail["coin"]["pool_address"] == "Pool123"
    failed = memecoins.refresh_memecoins(
        download=lambda *_: b'{"error":"rate limit"}', at=AT + timedelta(minutes=5)
    )
    assert failed["status"] == "error"
    saved = memecoins.memecoin_market(at=AT + timedelta(minutes=5))
    assert saved["collected_at"] == AT.isoformat()
    assert saved["refresh_failed"] is True
    assert saved["rows"][0]["observed_at"] == AT.isoformat()


def test_empty_pool_window_replaces_previous_discovery(market_db):  # noqa: F811
    memecoins.refresh_memecoins(download=lambda *_: json.dumps({"data": [pool()]}).encode(), at=AT)
    result = memecoins.refresh_memecoins(
        download=lambda *_: b'{"data":[]}', at=AT + timedelta(minutes=5)
    )
    assert result["status"] == "ok"
    assert memecoins.memecoin_market(at=AT + timedelta(minutes=5))["rows"] == []
