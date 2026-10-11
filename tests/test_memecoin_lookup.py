from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from runner_web import db
from runner_web import memecoin_lookup as lookup
from runner_web.db import connection, init_db
from runner_web.helius_discovery import _decode
from runner_web.solana_keys import TOKEN_2022_PROGRAM, TOKEN_PROGRAM

MINT = "ARPwPPWbaj3FYBf6k1Lt2jqRkv9JJUg3Hxav5aHKTEem"
OTHER = "So11111111111111111111111111111111111111112"
AT = datetime(2026, 10, 10, 12, tzinfo=UTC)


@pytest.fixture
def lookup_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "lookup.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    init_db()


def metadata_account(*, mint=MINT, mutable=True, creators=False):
    def string(value):
        raw = value.encode()
        return len(raw).to_bytes(4, "little") + raw

    raw = b"\x04" + _decode(OTHER, 44) + _decode(mint, 44)
    raw += string("Sample token") + string("SAMPLE") + string("https://example.com/token.json")
    raw += b"\0\0" + (b"\x01\x01\0\0\0" + _decode(OTHER, 44) + b"\x01\x64" if creators else b"\0")
    raw += bytes([0, mutable])
    return {"owner": lookup.METADATA_PROGRAM, "data": [base64.b64encode(raw).decode(), "base64"]}


def chain(*, metadata=True, program=TOKEN_PROGRAM, **info):
    return {
        "result": {
            "context": {"slot": 12345},
            "value": [
                {
                    "owner": program,
                    "data": {
                        "parsed": {
                            "type": "mint",
                            "info": {
                                "isInitialized": True,
                                "supply": "18446744073709551615",
                                "decimals": 9,
                                "mintAuthority": None,
                                "freezeAuthority": OTHER,
                                **info,
                            },
                        }
                    },
                },
                metadata_account() if metadata else None,
            ],
        }
    }


def market(mint=MINT, **attrs):
    return json.dumps(
        {
            "data": {
                "id": f"solana_{mint}",
                "attributes": {
                    "address": mint,
                    "name": "Market name",
                    "symbol": "MARKET",
                    "price_usd": "0.0000123",
                    "market_cap_usd": None,
                    "total_reserve_in_usd": "25000",
                    "volume_usd": {"h24": "12500"},
                    **attrs,
                },
            }
        }
    ).encode()


def facts(value):
    return {item["label"]: item["value"] for item in value["facts"]}


def failed(*args, **kwargs):
    raise TimeoutError("private-provider-url?api-key=SECRET")


def test_first_search_reads_mint_metadata_and_market_now(lookup_db):
    calls = []

    def rpc(body, **options):
        calls.append((body, options))
        return chain()

    value = lookup.lookup_memecoin(MINT, rpc=rpc, download=lambda *_: market(), at=AT)
    assert value["name"] == "Sample token" and value["symbol"] == "SAMPLE"
    assert value["status"] == "ready" and value["slot"] == 12345
    assert facts(value)["Supply"] == "18446744073.709551615"
    assert facts(value)["Mint authority"] == "Revoked"
    assert facts(value)["Freeze authority"] == OTHER
    assert facts(value)["Metadata edits"] == "Allowed"
    assert facts(value)["Metadata update authority"] == OTHER
    assert facts(value)["Price · GeckoTerminal"] == "$0.0000123"
    assert "Market cap" not in facts(value)
    assert calls[0][0]["method"] == "getMultipleAccounts"
    assert calls[0][0]["params"][0][0] == MINT
    assert len(calls[0][0]["params"][0]) == 2
    assert calls[0][1] == {"credits": 1, "timeout": 2.0}


def test_repeat_requests_reuse_shared_cache_and_refresh_after_expiry(lookup_db):
    value = lookup.lookup_memecoin(
        MINT, rpc=lambda *_args, **_kwargs: chain(), download=lambda *_: market(), at=AT
    )
    assert (
        lookup.lookup_memecoin(MINT, rpc=failed, download=failed, at=AT + timedelta(seconds=299))
        == value
    )
    refreshed = lookup.lookup_memecoin(
        MINT,
        rpc=lambda *_args, **_kwargs: chain(decimals=0),
        download=failed,
        at=AT + timedelta(seconds=301),
    )
    assert facts(refreshed)["Decimals"] == "0"


def test_metadata_and_controls_show_without_a_market_quote(lookup_db):
    value = lookup.lookup_memecoin(
        MINT, rpc=lambda *_args, **_kwargs: chain(), download=failed, at=AT
    )
    assert value["name"] == "Sample token"
    assert facts(value)["Mint authority"] == "Revoked"
    assert "Price · GeckoTerminal" not in facts(value)
    assert "SECRET" not in json.dumps(value)


def test_market_survives_a_failed_chain_read_and_keeps_controls_pending(lookup_db):
    value = lookup.lookup_memecoin(MINT, rpc=failed, download=lambda *_: market(), at=AT)
    assert value["name"] == "Market name" and value["status"] == "pending"
    assert facts(value)["Mint authority"] == "Pending"
    assert facts(value)["Price · GeckoTerminal"] == "$0.0000123"
    assert "SECRET" not in json.dumps(value)


def test_timeouts_are_cached_and_do_not_repeat_before_retry_window(lookup_db):
    calls = []

    def rpc(*_args, **_kwargs):
        calls.append(True)
        return failed()

    value = lookup.lookup_memecoin(MINT, rpc=rpc, download=failed, at=AT)
    assert value["status"] == "pending"
    assert lookup.lookup_memecoin(MINT, rpc=rpc, download=failed, at=AT) == value
    assert len(calls) == 1


@pytest.mark.parametrize(
    "account", [None, {"owner": "11111111111111111111111111111111", "data": ["", "base64"]}]
)
def test_wallets_and_missing_accounts_get_an_address_check(lookup_db, account):
    def unused(*_args):
        pytest.fail("A non-mint needs no market lookup")

    value = lookup.lookup_memecoin(
        MINT,
        rpc=lambda *_args, **_kwargs: {"result": {"value": [account, None]}},
        download=unused,
        at=AT,
    )
    assert value["status"] == "invalid" and value["facts"] == []


def test_missing_permissions_stay_pending(lookup_db):
    payload = chain(metadata=False)
    del payload["result"]["value"][0]["data"]["parsed"]["info"]["mintAuthority"]
    value = lookup.lookup_memecoin(
        MINT, rpc=lambda *_args, **_kwargs: payload, download=failed, at=AT
    )
    assert facts(value)["Mint authority"] == "Pending"
    assert facts(value)["Metadata controls"] == "Pending"


def test_unparsed_token_accounts_keep_the_read_pending(lookup_db):
    payload = chain()
    payload["result"]["value"][0]["data"] = ["", "base64"]
    value = lookup.lookup_memecoin(
        MINT, rpc=lambda *_args, **_kwargs: payload, download=lambda *_args: market(), at=AT
    )
    assert value["status"] == "pending"
    assert facts(value)["Mint authority"] == "Pending"
    assert facts(value)["Price · GeckoTerminal"] == "$0.0000123"


def test_market_read_uses_the_remaining_request_budget(lookup_db, monkeypatch):
    moments = iter([0, lookup.LOOKUP_SECONDS + 0.1])
    monkeypatch.setattr(lookup.time, "monotonic", lambda: next(moments))
    calls = []
    value = lookup.lookup_memecoin(
        MINT,
        rpc=lambda *_args, **_kwargs: chain(),
        download=lambda *_args: calls.append(True),
        at=AT,
    )
    assert value["status"] == "ready" and calls == []


def test_token_2022_metadata_and_pointer_controls_remain_separate(lookup_db):
    payload = chain(
        metadata=False,
        program=TOKEN_2022_PROGRAM,
        extensions=[
            {
                "extension": "tokenMetadata",
                "state": {"mint": MINT, "name": "Inline", "symbol": "IN", "updateAuthority": None},
            },
            {"extension": "metadataPointer", "state": {"authority": OTHER}},
        ],
    )
    value = lookup.lookup_memecoin(
        MINT, rpc=lambda *_args, **_kwargs: payload, download=failed, at=AT
    )
    assert value["name"] == "Inline"
    assert facts(value)["Metadata edits"] == "Locked"
    assert facts(value)["Metadata update authority"] == "Revoked"
    assert facts(value)["Metadata pointer authority"] == OTHER
    assert facts(value)["Token program"] == "Token-2022"


@pytest.mark.parametrize("authority", [True, "a" * 44])
def test_malformed_metadata_permissions_stay_pending(lookup_db, authority):
    payload = chain(
        metadata=False,
        program=TOKEN_2022_PROGRAM,
        extensions=[
            {
                "extension": "tokenMetadata",
                "state": {"mint": MINT, "updateAuthority": authority},
            }
        ],
    )
    value = lookup.lookup_memecoin(
        MINT, rpc=lambda *_args, **_kwargs: payload, download=failed, at=AT
    )
    assert facts(value)["Metadata edits"] == "Pending"
    assert facts(value)["Metadata update authority"] == "Pending"


@pytest.mark.parametrize("creators", [False, True])
def test_metadata_mutability_uses_the_actual_borsh_layout(creators):
    metadata = lookup._metadata(metadata_account(mutable=False, creators=creators), MINT)
    assert metadata["mutable"] is False and metadata["name"] == "Sample token"
    assert lookup._metadata(metadata_account(mint=OTHER), MINT) is None
    account = metadata_account()
    account["data"][0] = base64.b64encode(base64.b64decode(account["data"][0])[:-1]).decode()
    assert lookup._metadata(account, MINT) is None


def test_cache_lease_coalesces_simultaneous_searches(lookup_db):
    def rpc(*_args, **_kwargs):
        waiting = lookup.lookup_memecoin(MINT, rpc=failed, download=failed, at=AT)
        assert waiting["status"] == "pending"
        assert waiting["checked_at"] is None
        return chain()

    assert lookup.lookup_memecoin(MINT, rpc=rpc, download=failed, at=AT)["status"] == "ready"


def test_shared_attempt_cap_also_bounds_repeat_addresses(lookup_db, monkeypatch):
    monkeypatch.setattr(lookup, "MAX_ATTEMPTS", 1)
    lookup.lookup_memecoin(MINT, rpc=failed, download=failed, at=AT)

    def unused(*_args, **_kwargs):
        pytest.fail("The shared cap should keep this read pending")

    value = lookup.lookup_memecoin(OTHER, rpc=unused, download=unused, at=AT)
    assert value["status"] == "pending" and value["checked_at"] is None
    value = lookup.lookup_memecoin(MINT, rpc=unused, download=unused, at=AT + timedelta(seconds=61))
    assert value["status"] == "pending"


@pytest.mark.parametrize("query", ["new token", "a" * 44, "1" * 31, "z" * 44])
def test_invalid_searches_skip_providers_and_storage(query):
    assert lookup.lookup_memecoin(query, rpc=failed, download=failed) is None


def test_market_response_must_bind_the_exact_searched_address():
    with pytest.raises(ValueError, match="differs"):
        lookup._market_basics(MINT, market(OTHER))


def test_paused_collection_uses_cache_without_provider_reads(lookup_db, monkeypatch):
    value = lookup.lookup_memecoin(
        MINT, rpc=lambda *_args, **_kwargs: chain(), download=failed, at=AT
    )
    monkeypatch.setenv("MEMECOINS_ENABLED", "false")
    assert lookup.lookup_memecoin(OTHER, rpc=failed, download=failed, at=AT) is None
    assert (
        lookup.lookup_memecoin(MINT, rpc=failed, download=failed, at=AT + timedelta(seconds=301))
        == value
    )


def test_page_and_api_hydrate_on_first_search_and_keep_worker_queue(lookup_db, monkeypatch):
    from runner_web import main

    reads = []
    monkeypatch.setattr(main, "enforce_rate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "current_user", lambda *_args: None)

    def rpc(*_args, **_kwargs):
        reads.append(True)
        return chain()

    monkeypatch.setattr(lookup, "rpc_request", rpc)
    monkeypatch.setattr(lookup, "_download", lambda *_args: market())
    client = TestClient(main.app)
    page = client.get("/memecoins", params={"q": MINT})
    api = client.get("/api/memecoins", params={"q": MINT})
    assert page.status_code == api.status_code == 200
    assert "Sample token" in page.text and "18446744073.709551615" in page.text
    assert "data-token-lookup" in page.text and "Search again then" not in page.text
    assert api.json()["requested"] is True
    assert api.json()["token_lookup"]["name"] == "Sample token"
    assert len(reads) == 1
    with connection() as database:
        queue = database.execute(
            "SELECT value FROM worker_state WHERE key='memecoin_searched'"
        ).fetchone()
    assert MINT in json.loads(queue["value"])
