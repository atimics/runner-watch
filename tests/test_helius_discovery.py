import io
import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError

import pytest

from runner_web import db, memecoins
from runner_web import helius_discovery as helius
from runner_web.memecoin_integrity import SELL, creator_trades

AT = datetime(2026, 9, 8, 17, tzinfo=UTC)
POOL = helius.PUMP_SWAP
MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CREATOR = "So11111111111111111111111111111111111111112"
OTHER = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SIGNATURE = "1" * 64


def creation():
    data = helius.CREATE_POOL + bytes(18) + helius._decode(CREATOR)
    return {
        "slot": 100,
        "blockTime": int(AT.timestamp()) - 120,
        "meta": {"err": None, "innerInstructions": []},
        "transaction": {
            "signatures": [SIGNATURE],
            "message": {
                "instructions": [
                    {
                        "programId": POOL,
                        "accounts": [POOL, OTHER, CREATOR, MINT, OTHER],
                        "data": helius._encode(data),
                    }
                ]
            },
        },
    }


def sale():
    tx = creation()
    tx["slot"] = 101
    tx["blockTime"] += 10
    tx["transaction"]["signatures"] = [helius._encode(bytes([2]) * 64)]
    tx["transaction"]["message"]["instructions"] = [
        {
            "programId": POOL,
            "accounts": [POOL, CREATOR, OTHER, MINT, OTHER],
            "data": helius._encode(SELL + bytes(16)),
        }
    ]

    def balance(amount):
        return {
            "owner": CREATOR,
            "mint": MINT,
            "uiTokenAmount": {"amount": str(amount), "decimals": 6},
        }

    tx["meta"].update(preTokenBalances=[balance(9000000)], postTokenBalances=[balance(3000000)])
    return tx


def reply(entries, cursor=None):
    return {"result": {"data": entries, "paginationToken": cursor}}


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "helius.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setenv("MEMECOINS_ENABLED", "true")
    db.init_db()


def test_creation_has_finalized_identity_and_transaction_receipt():
    row = helius.pool_creations([creation()], at=AT)[0]
    assert row["token_address"] == MINT
    assert row["pool_creator"] == CREATOR
    assert row["declared_creator"] == CREATOR
    assert row["slot"] == 100
    assert row["source_url"] == "https://solscan.io/tx/" + SIGNATURE
    nested = creation()
    nested["meta"]["innerInstructions"] = [
        {"instructions": nested["transaction"]["message"]["instructions"]}
    ]
    nested["transaction"]["message"]["instructions"] = []
    assert helius.pool_creations([nested, creation()], at=AT) == [row]


@pytest.mark.parametrize(
    "change", ["failed", "wrong_program", "wrong_type", "bad_address", "future"]
)
def test_unrelated_or_invalid_transactions_cannot_create_candidates(change):
    tx = creation()
    ix = tx["transaction"]["message"]["instructions"][0]
    if change == "failed":
        tx["meta"]["err"] = {"InstructionError": [0, "failed"]}
    if change == "wrong_program":
        ix["programId"] = OTHER
    if change == "wrong_type":
        ix["data"] = helius._encode(SELL)
    if change == "bad_address":
        ix["accounts"][3] = "../bad"
    if change == "future":
        tx["blockTime"] = int(AT.timestamp()) + 10
    assert helius.pool_creations([tx, None, {}], at=AT) == []


def test_pagination_is_bounded_and_reports_partial_coverage():
    requests = []

    def rpc(body):
        requests.append(body)
        return reply([creation()], str(len(requests)))

    result = helius.discover_pools(at=AT, rpc=rpc)
    assert len(requests) == 2
    assert result["partial"] is True
    assert len(result["pools"]) == 1
    assert requests[0]["params"][1]["commitment"] == "finalized"
    assert requests[0]["params"][1]["encoding"] == "jsonParsed"
    assert requests[0]["params"][1]["maxSupportedTransactionVersion"] == 1
    assert requests[1]["params"][1]["paginationToken"] == "1"
    assert requests[0]["params"][1]["limit"] == 100


@pytest.mark.parametrize(
    "payload", [{"error": {"message": "private"}}, {}, {"result": {"data": None}}]
)
def test_rpc_errors_are_rejected(payload):
    with pytest.raises(ValueError):
        helius.discover_pools(at=AT, rpc=lambda _: payload)


def test_api_key_and_provider_error_are_private(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "private-test-key")

    def fail(request, **_):
        assert "api-key=private-test-key" in request.full_url
        raise HTTPError(request.full_url, 403, "private-test-key", {}, None)

    monkeypatch.setattr(helius.urllib.request, "urlopen", fail)
    with pytest.raises(ValueError, match="Helius request failed") as error:
        helius.rpc_request({})
    assert "private-test-key" not in str(error.value)
    monkeypatch.delenv("HELIUS_API_KEY")
    with pytest.raises(ValueError, match="HELIUS_API_KEY is required"):
        helius.rpc_request({})


def test_rpc_posts_json_and_validates_response(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "test-key")

    def open_request(request, **_):
        assert request.method == "POST"
        assert json.loads(request.data)["method"] == "getTransactionsForAddress"
        return io.BytesIO(json.dumps(reply([])).encode())

    monkeypatch.setattr(helius.urllib.request, "urlopen", open_request)
    assert helius.discover_pools(at=AT)["pools"] == []


def test_creator_sale_has_two_receipts_and_exact_net_amount():
    pools = helius.pool_creations([creation()], at=AT)
    alerts = creator_trades([sale(), sale()], pools, at=AT)
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "creator_sell"
    assert alerts[0]["net_token_amount"] == "6.000000"
    assert alerts[0]["roles"] == ["pool_creator", "declared_creator"]
    assert alerts[0]["relationship_source_url"] == pools[0]["source_url"]
    assert alerts[0]["source_url"] != pools[0]["source_url"]


@pytest.mark.parametrize(
    "change", ["transfer", "other_wallet", "failed", "net_increase", "earlier"]
)
def test_sale_alert_needs_matching_trade_wallet_and_balance_change(change):
    tx = sale()
    if change == "transfer":
        tx["transaction"]["message"]["instructions"][0]["data"] = "11111111"
    if change == "other_wallet":
        tx["transaction"]["message"]["instructions"][0]["accounts"][1] = OTHER
    if change == "failed":
        tx["meta"]["err"] = "failed"
    if change == "net_increase":
        tx["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = "99999999"
    if change == "earlier":
        tx["slot"] = 99
    assert creator_trades([tx], helius.pool_creations([creation()], at=AT), at=AT) == []


def test_full_refresh_keeps_evidence_before_quotes_arrive(database):
    requests = []

    def download(url, timeout):
        requests.append(url)
        return b'{"data":[]}'

    result = memecoins.refresh_memecoins(
        at=AT, rpc=lambda _: reply([creation(), sale()]), download=download
    )
    assert result["status"] == "ok"
    assert requests == ["https://api.geckoterminal.com/api/v2/networks/solana/pools/multi/" + POOL]
    market = memecoins.memecoin_market(at=AT)
    assert market["rows"] == []
    assert market["integrity_alerts"][0]["net_token_amount"] == "6.000000"
    assert market["integrity_coverage"]["received_transactions"] == 6
    again = memecoins.refresh_memecoins(
        at=AT + timedelta(minutes=5), rpc=lambda _: reply([sale()]), download=download
    )
    assert again["status"] == "ok"
    assert len(memecoins.memecoin_market(at=AT)["integrity_alerts"]) == 1
    failed = memecoins.refresh_memecoins(
        at=AT + timedelta(minutes=10), rpc=lambda _: {"error": {}}, download=download
    )
    assert failed["status"] == "error"
    assert memecoins.memecoin_market(at=AT)["integrity_alerts"][0]["signature"]


def test_quote_provider_cannot_add_discovery_candidates(database):
    from runner_web.memecoins import _collect_helius

    quote = {"attributes": {"address": OTHER}, "relationships": {}}
    rows, _ = _collect_helius(
        at=AT,
        rpc=lambda _: reply([creation()]),
        download=lambda *_: json.dumps({"data": [quote]}).encode(),
    )
    assert rows == []


CURVE = "6nRYepjJzCK1fLc9C3kcHjRJ92wzgyqNFZwGAP5TM3rU"


def launch():
    from runner_web.memecoin_chain_parser import PUMP, PUMP_CREATE_V2

    tx = creation()
    tx["slot"] = 90
    tx["blockTime"] -= 60
    tx["transaction"]["signatures"] = [helius._encode(bytes([3]) * 64)]
    raw = PUMP_CREATE_V2
    for value in (b"name", b"SYM", b"https://example.invalid"):
        raw += len(value).to_bytes(4, "little") + value
    tx["transaction"]["message"]["instructions"] = [
        {
            "programId": PUMP,
            # create_v2: mint, mint authority, bonding curve, curve token account, ..., user.
            "accounts": [MINT, OTHER, CURVE, OTHER, OTHER, CREATOR],
            "data": helius._encode(raw + helius._decode(CREATOR) + bytes(2)),
        }
    ]
    return tx


def curve_quote():
    return {
        "attributes": {
            "address": CURVE,
            "base_token_price_usd": "0.00001",
            "reserve_in_usd": "8000",
            "pool_created_at": (AT - timedelta(minutes=3)).isoformat(),
            "price_change_percentage": {"m5": "3", "h1": "3", "h6": "3", "h24": "3"},
            "volume_usd": {"m5": "900", "h1": "1200", "h6": "1200", "h24": "1200"},
            "transactions": {
                window: {"buys": 30, "sells": 5, "buyers": 25, "sellers": 4}
                for window in ("m5", "h1", "h6", "h24")
            },
        },
        "relationships": {"base_token": {"data": {"id": "solana_" + MINT}}},
    }


def test_a_launch_is_quoted_on_its_bonding_curve_until_it_graduates(database):
    from runner_web.memecoins import _collect_helius

    requests = []

    def download(url, timeout):
        requests.append(url)
        return json.dumps({"data": [curve_quote()] if CURVE in url else []}).encode()

    rows, metadata = _collect_helius(at=AT, rpc=lambda _: reply([launch()]), download=download)

    assert [row["pool_address"] for row in rows] == [CURVE]
    assert rows[0]["venue"] == "bonding_curve"
    assert rows[0]["early"]["features"]["on_curve"] is True
    # A young curve is not ruled out as a thin pool.
    assert "Pool is too thin" not in rows[0]["early"]["reasons"]
    assert metadata["tracked_curves"] == 1

    # Once the coin graduates to a pool, its curve is no longer quoted.
    requests.clear()
    later = AT + timedelta(minutes=5)
    graduation = creation()
    graduation["blockTime"] = int(later.timestamp()) - 120
    _collect_helius(at=later, rpc=lambda _: reply([graduation]), download=download)
    assert requests and not any(CURVE in url for url in requests)


def test_a_full_pool_list_and_curve_list_are_quoted_in_one_pass(database):
    """Regression: 100 pools plus 90 curves once broke the 100-record check."""

    from runner_web.memecoins import MAX_QUOTED_POOLS, normalize_chain_pools

    assert normalize_chain_pools({"data": []}, at=AT) == []
    with pytest.raises(ValueError):
        normalize_chain_pools({"data": [{}] * (MAX_QUOTED_POOLS + 1)}, at=AT)
    # A full pass of pools and curves is within the limit.
    assert normalize_chain_pools({"data": [{}] * MAX_QUOTED_POOLS}, at=AT) == []


def _many_launches(count):
    launches, mints = [], {}
    for number in range(count):
        tx = launch()
        tx["slot"] = 50 + number
        tx["transaction"]["signatures"] = [helius._encode(number.to_bytes(2, "big") * 32)]
        mint = helius._encode(bytes([7, number]) * 16)
        curve = helius._encode(bytes([8, number]) * 16)
        mints[curve] = mint
        ix = tx["transaction"]["message"]["instructions"][0]
        ix["accounts"] = [mint, OTHER, curve, OTHER, OTHER, CREATOR]
        launches.append(tx)
    return launches, mints


def _rate_limited(url):
    return HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))


def test_a_rate_limit_on_curves_keeps_the_pools_and_the_refresh(database, monkeypatch):
    sleeps = []
    monkeypatch.setattr(memecoins.time, "sleep", sleeps.append)
    launches, _ = _many_launches(40)
    requests = []

    def download(url, timeout):
        if "/search/" in url:
            return b'{"data":[]}'
        requests.append(url)
        if len(requests) > 1:
            raise _rate_limited(url)
        return b'{"data":[]}'

    result = memecoins.refresh_memecoins(
        at=AT, rpc=lambda _: reply(launches + [creation()]), download=download
    )

    assert result["status"] == "ok"
    # The graduated pool is asked for first; the curves come after it.
    assert requests[0].rsplit("/", 1)[1].split(",")[0] == POOL
    # Every request after the first waits, so GeckoTerminal does not see a burst.
    assert set(sleeps) == {memecoins.QUOTE_PAUSE_SECONDS}


def test_a_rate_limit_on_pools_retries_once_then_fails_and_is_logged(database, caplog, monkeypatch):
    sleeps = []
    monkeypatch.setattr(memecoins.time, "sleep", sleeps.append)
    requests = []

    def download(url, timeout):
        requests.append(url)
        raise _rate_limited(url)

    result = memecoins.refresh_memecoins(
        at=AT, rpc=lambda _: reply([creation()]), download=download
    )

    assert result["status"] == "error"
    assert "Memecoin refresh failed" in caplog.text
    assert len(requests) == 2 and sleeps == [memecoins.RATE_LIMIT_RETRY_SECONDS]


def test_a_rate_limited_pool_batch_that_clears_keeps_the_refresh(database, monkeypatch):
    monkeypatch.setattr(memecoins.time, "sleep", lambda _: None)
    requests = []

    def download(url, timeout):
        requests.append(url)
        if len(requests) == 1:
            raise _rate_limited(url)
        return b'{"data":[]}'

    result = memecoins.refresh_memecoins(
        at=AT, rpc=lambda _: reply([creation()]), download=download
    )

    assert result["status"] == "ok" and len(requests) == 2


def test_a_full_curve_watch_still_refreshes(database, monkeypatch):
    from runner_web.memecoins import CURVE_SLOTS

    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)

    launches, mints = _many_launches(CURVE_SLOTS + 5)
    requests = []

    def download(url, timeout):
        # Every curve trades, as a busy cycle would return.
        requests.append(url)
        quotes = []
        for address in url.rsplit("/", 1)[1].split(","):
            if address in mints:
                quote = curve_quote()
                quote["attributes"]["address"] = address
                quote["relationships"]["base_token"]["data"]["id"] = "solana_" + mints[address]
                quotes.append(quote)
        return json.dumps({"data": quotes}).encode()

    result = memecoins.refresh_memecoins(
        at=AT, rpc=lambda _: reply(launches + [creation()]), download=download
    )

    assert result["status"] == "ok"
    assert result["count"] == CURVE_SLOTS


def test_copied_launch_names_bring_the_original_onto_the_board(database, monkeypatch):
    from runner_web.memecoin_chain_parser import PUMP_CREATE_V2

    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)
    original = helius._encode(bytes([9]) * 32)
    original_pool = helius._encode(bytes([10]) * 32)
    launches, mints = _many_launches(3)
    for number, tx in enumerate(launches):
        raw = PUMP_CREATE_V2
        for value in (["Trolloween", "TR0LLOWEEN", "tro11oween"][number].encode(), b"TROLL", b""):
            raw += len(value).to_bytes(4, "little") + value
        ix = tx["transaction"]["message"]["instructions"][0]
        ix["data"] = helius._encode(raw + helius._decode(CREATOR) + bytes(2))
    searches = []

    def download(url, timeout):
        if "/search/" in url:
            searches.append(url)
            pool = curve_quote()
            pool["attributes"].update(
                address=original_pool, name="TROLLOWEEN / SOL", reserve_in_usd="14000"
            )
            pool["attributes"]["pool_created_at"] = "2025-10-07T12:00:00Z"
            pool["relationships"]["base_token"]["data"]["id"] = "solana_" + original
            token = {"name": "TROLLOWEEN", "symbol": "TROLLOWEEN"}
            included = [{"type": "token", "id": "solana_" + original, "attributes": token}]
            return json.dumps({"data": [pool], "included": included}).encode()
        quotes = []
        for address in url.rsplit("/", 1)[1].split(","):
            quote = curve_quote()
            quote["attributes"]["address"] = address
            if address == original_pool:
                quote["attributes"].update(
                    reserve_in_usd="14000", pool_created_at="2025-10-07T12:00:00Z"
                )
                quote["relationships"]["base_token"]["data"]["id"] = "solana_" + original
            elif address in mints:
                quote["relationships"]["base_token"]["data"]["id"] = "solana_" + mints[address]
            quotes.append(quote)
        return json.dumps({"data": quotes}).encode()

    rows, _ = memecoins._collect_helius(at=AT, rpc=lambda _: reply(launches), download=download)

    # The search runs after the pools are quoted; the original joins next cycle.
    assert len(searches) == 1 and "query=Trolloween&" in searches[0]
    assert original not in {row["token_address"] for row in rows}
    later = AT + timedelta(minutes=5)
    rows, _ = memecoins._collect_helius(at=later, rpc=lambda _: reply([]), download=download)
    assert len(searches) == 1
    by_token = {row["token_address"]: row for row in rows}
    assert by_token[original]["discovery_source"] == "GeckoTerminal name search"
    assert by_token[original]["early"]["state"] == "setup"
    assert by_token[original]["early"]["reasons"][0].startswith("3 copycat launches in 24h")
    copies = [row for row in rows if row["token_address"] != original]
    assert len(copies) == 3
    assert all(row["early"]["state"] == "avoid" for row in copies)


def test_a_searched_address_is_quoted_on_its_busiest_pool(database, monkeypatch):
    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)
    searched = "ARPwPPWbaj3FYBf6k1Lt2jqRkv9JJUg3Hxav5aHKTEem"
    busy, quiet = (
        "AKKjxxmVZyrBusyPool11111111111111111111111",
        "7BNQn5JaJcQuietPool1111111111111111111111",
    )
    memecoins.request_memecoin(searched, at=AT - timedelta(minutes=1))
    requests = []

    def download(url, timeout):
        requests.append(url)
        if "/tokens/multi/" in url:
            return json.dumps(
                {
                    "data": [
                        {
                            "id": "solana_" + searched,
                            "relationships": {
                                "top_pools": {
                                    "data": [{"id": "solana_" + quiet}, {"id": "solana_" + busy}]
                                }
                            },
                        }
                    ],
                    "included": [
                        {
                            "type": "pool",
                            "id": "solana_" + busy,
                            "attributes": {"reserve_in_usd": "21930"},
                        },
                        {
                            "type": "pool",
                            "id": "solana_" + quiet,
                            "attributes": {"reserve_in_usd": "545"},
                        },
                    ],
                }
            ).encode()
        quote = curve_quote()
        quote["attributes"]["address"] = busy
        quote["relationships"]["base_token"]["data"]["id"] = "solana_" + searched
        return json.dumps({"data": [quote] if busy in url else []}).encode()

    rows, _ = memecoins._collect_helius(at=AT, rpc=lambda _: reply([]), download=download)

    assert [row["token_address"] for row in rows] == [searched]
    assert rows[0]["pool_address"] == busy
    assert rows[0]["discovery_source"] == "Searched by address"


def test_a_failed_search_lookup_keeps_the_refresh(database, monkeypatch):
    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)
    memecoins.request_memecoin("ARPwPPWbaj3FYBf6k1Lt2jqRkv9JJUg3Hxav5aHKTEem", at=AT)

    def download(url, timeout):
        if "/tokens/multi/" in url:
            raise HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))
        return b'{"data":[]}'

    result = memecoins.refresh_memecoins(
        at=AT, rpc=lambda _: reply([creation()]), download=download
    )

    assert result["status"] == "ok"


def test_graduations_are_read_every_third_run_within_the_same_pages(database):
    from runner_web.memecoin_chain_ingestion import MIGRATION_AUTHORITY, collect_chain

    runs = []
    for number in range(3):
        addresses = []

        def rpc(body, addresses=addresses):
            addresses.append(body["params"][0])
            return reply([])

        collect_chain(at=AT + timedelta(minutes=5 * number), rpc=rpc)
        runs.append(addresses)

    # Two pages and at most one wallet page each run, so the credit cost is unchanged.
    assert all(len(addresses) <= 3 for addresses in runs)
    assert [MIGRATION_AUTHORITY in addresses for addresses in runs] == [True, False, False]
    # The launch stream takes the same kind of slot in another run.
    from runner_web.memecoin_chain_ingestion import MINT_AUTHORITY

    assert [MINT_AUTHORITY in addresses for addresses in runs] == [False, False, True]


def test_a_graduation_creates_its_pool_from_the_migration(database):
    from runner_web.memecoin_chain_ingestion import MIGRATION_AUTHORITY, collect_chain
    from runner_web.memecoin_chain_parser import PUMP

    graduation = creation()
    message = graduation["transaction"]["message"]
    pool_creation = message["instructions"][0]
    # The pool is created inside Pump's migrate instruction, not at the top level.
    message["instructions"] = [
        {
            "programId": PUMP,
            "accounts": [OTHER, MIGRATION_AUTHORITY, MINT],
            "data": helius._encode(bytes([155, 234, 231, 146, 236, 158, 162, 30])),
        }
    ]
    graduation["meta"]["innerInstructions"] = [{"index": 0, "instructions": [pool_creation]}]

    def rpc(body):
        return reply([graduation] if body["params"][0] == MIGRATION_AUTHORITY else [])

    pools = collect_chain(at=AT, rpc=rpc)["pools"]

    assert [(pool["pool_address"], pool["token_address"]) for pool in pools] == [(POOL, MINT)]


def test_a_pool_that_traded_keeps_its_slot_against_newer_graduations(database):
    from runner_web.memecoins import POOL_SLOTS, _watch

    def pool(number, minutes_ago):
        created = (AT - timedelta(minutes=minutes_ago)).isoformat()
        return {"pool_address": f"pool-{number}", "created_at": created}

    runner = pool(0, 600)
    newer = [pool(number, number) for number in range(1, POOL_SLOTS + 20)]

    chosen = _watch(newer + [runner], [runner], slots=POOL_SLOTS, active_slots=60, is_open=bool)

    assert len(chosen) == POOL_SLOTS
    assert chosen[0] is runner
    assert chosen[1]["pool_address"] == "pool-1"


def _chain_rpc(accounts):
    """Transaction pages carry the pool creation; account reads answer from `accounts`."""

    def rpc(body, credits=None):
        if body["method"] == "getMultipleAccounts":
            return {"result": {"value": [accounts.get(a) for a in body["params"][0]]}}
        return reply([creation()])

    return rpc


def _pool_accounts():
    from tests.test_memecoin_chain_prices import (
        BASE_VAULT,
        QUOTE_VAULT,
        SOL_VAULT,
        USDC_VAULT,
        pool_account,
        token_account,
    )

    return {
        SOL_VAULT: token_account(1_000 * 10**9, 9),
        USDC_VAULT: token_account(121_290 * 10**6, 6),
        POOL: pool_account(base_mint=MINT, virtual=0),
        BASE_VAULT: token_account(1_000_000 * 10**6, 6),
        QUOTE_VAULT: token_account(100 * 10**9, 9),
    }


def _gecko_pool(price="0.5"):
    quote = curve_quote()
    quote["attributes"].update(address=POOL, base_token_price_usd=price, reserve_in_usd="30000")
    quote["relationships"]["base_token"]["data"]["id"] = "solana_" + MINT
    return quote


CHAIN_PRICE = 100 / 1_000_000 * 121.29


def test_shadow_mode_keeps_gecko_prices_and_records_the_gap(database, monkeypatch):
    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)
    monkeypatch.setenv("MEMECOIN_PRICE_SOURCE", "shadow")
    download = lambda *_: json.dumps({"data": [_gecko_pool(str(CHAIN_PRICE * 1.02))]}).encode()  # noqa: E731

    rows, _ = memecoins._collect_helius(at=AT, rpc=_chain_rpc(_pool_accounts()), download=download)

    assert rows[0]["price"] == pytest.approx(CHAIN_PRICE * 1.02)
    check = memecoins._market_states(keys=("memecoin_price_check",))["memecoin_price_check"]
    assert check["compared"] == 1 and check["median_gap_pct"] == pytest.approx(1.96, abs=0.01)


def test_chain_mode_prices_from_the_pool_and_keeps_gecko_activity(database, monkeypatch):
    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)
    monkeypatch.setenv("MEMECOIN_PRICE_SOURCE", "chain")
    download = lambda *_: json.dumps({"data": [_gecko_pool()]}).encode()  # noqa: E731

    rows, _ = memecoins._collect_helius(at=AT, rpc=_chain_rpc(_pool_accounts()), download=download)

    assert rows[0]["price"] == pytest.approx(CHAIN_PRICE)
    assert rows[0]["liquidity_usd"] == pytest.approx(2 * 100 * 121.29)
    # The new pool was shortlisted, so GeckoTerminal's windows are kept.
    assert rows[0]["volume_h1"] == 1200.0


def test_chain_mode_shows_a_pool_geckoterminal_did_not_quote(database, monkeypatch):
    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)
    monkeypatch.setenv("MEMECOIN_PRICE_SOURCE", "chain")

    rows, _ = memecoins._collect_helius(
        at=AT, rpc=_chain_rpc(_pool_accounts()), download=lambda *_: b'{"data":[]}'
    )

    assert rows[0]["price"] == pytest.approx(CHAIN_PRICE)
    assert rows[0]["source"] == "Solana (Helius)" and rows[0]["volume_24h"] is None


def test_chain_mode_falls_back_to_gecko_when_the_chain_read_fails(database, monkeypatch):
    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)
    monkeypatch.setenv("MEMECOIN_PRICE_SOURCE", "chain")
    accounts = _pool_accounts()
    accounts.pop(next(iter(accounts)))  # no SOL price

    rows, _ = memecoins._collect_helius(
        at=AT,
        rpc=_chain_rpc(accounts),
        download=lambda *_: json.dumps({"data": [_gecko_pool()]}).encode(),
    )

    assert rows[0]["price"] == 0.5


def test_chain_mode_keeps_a_pool_whose_chain_price_moved(database, monkeypatch):
    from tests.test_memecoin_chain_prices import QUOTE_VAULT, token_account

    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)
    monkeypatch.setenv("MEMECOIN_PRICE_SOURCE", "chain")
    accounts = _pool_accounts()
    no_activity = lambda *_: b'{"data":[]}'  # noqa: E731

    memecoins.refresh_memecoins(at=AT, rpc=_chain_rpc(accounts), download=no_activity)
    accounts[QUOTE_VAULT] = token_account(110 * 10**9, 9)
    memecoins.refresh_memecoins(
        at=AT + timedelta(minutes=5), rpc=_chain_rpc(accounts), download=no_activity
    )

    # No GeckoTerminal volume, but the price moved 10%: the pool keeps its slot.
    watch = memecoins._saved_list("memecoin_pool_watch")
    assert [pool["pool_address"] for pool in watch] == [POOL]


def test_a_chain_priced_row_names_both_sources(database, monkeypatch):
    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)
    monkeypatch.setenv("MEMECOIN_PRICE_SOURCE", "chain")

    rows, _ = memecoins._collect_helius(
        at=AT,
        rpc=_chain_rpc(_pool_accounts()),
        download=lambda *_: json.dumps({"data": [_gecko_pool()]}).encode(),
    )

    assert rows[0]["source"] == "Solana (Helius)"
    assert rows[0]["activity_source"] == "GeckoTerminal"


@pytest.mark.parametrize(
    ("path", "reading"),
    [
        ("/memecoins", True),
        ("/memecoins/coin/chain-abc", True),
        ("/api/memecoins", True),
        ("/api/screens/memecoins/chain-abc/detail", True),
        # Telegram fetches these when our own bot posts; they are not readers.
        ("/memecoins/coin/chain-abc/card.png", False),
        ("/api/memecoins/chain-abc/replays/r1.gif", False),
        ("/", False),
    ],
)
def test_which_requests_count_as_someone_reading(path, reading):
    assert memecoins.is_memecoin_view(path) is reading


def test_a_process_records_a_view_at_most_once_a_minute(monkeypatch):
    monkeypatch.setattr(memecoins, "_view_noted_at", 0.0)

    assert memecoins.view_note_due(1000.0)
    assert not memecoins.view_note_due(1030.0)
    assert memecoins.view_note_due(1061.0)


def test_quiet_after_half_an_hour_unread(database):
    assert not memecoins.memecoins_quiet(AT)  # nothing recorded yet: sample in full
    memecoins.note_memecoin_view(at=AT)

    assert not memecoins.memecoins_quiet(AT + timedelta(minutes=29))
    assert memecoins.memecoins_quiet(AT + timedelta(minutes=31))


def test_quiet_runs_read_only_the_graduations(database):
    from runner_web.memecoin_chain_ingestion import MIGRATION_AUTHORITY, collect_chain

    runs = []
    for number in range(3):
        addresses = []

        def rpc(body, addresses=addresses):
            addresses.append(body["params"][0])
            return reply([sale()])

        collect_chain(at=AT + timedelta(minutes=5 * number), rpc=rpc, quiet=True)
        runs.append(addresses)

    # No program or wallet samples: one graduation page every third run.
    assert runs == [[MIGRATION_AUTHORITY], [], []]


def test_a_view_on_the_site_keeps_the_worker_sampling(database, monkeypatch):
    from fastapi.testclient import TestClient

    from runner_web import main

    monkeypatch.setattr(memecoins, "_view_noted_at", 0.0)
    monkeypatch.setattr(main, "enforce_rate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "current_user", lambda *_: None)
    client = TestClient(main.app)

    client.get("/memecoins/coin/chain-missing/card.png")
    assert memecoins._market_states(keys=("memecoin_last_view",)) == {}
    assert client.get("/memecoins").status_code == 200
    assert memecoins._market_states(keys=("memecoin_last_view",))["memecoin_last_view"]


def test_a_creator_sell_turns_the_coin_avoid_in_the_same_cycle(database, monkeypatch):
    from runner_web.memecoin_watch import _finding

    monkeypatch.setattr(memecoins, "QUOTE_PAUSE_SECONDS", 0)
    monkeypatch.setenv("MEMECOIN_PRICE_SOURCE", "chain")
    sell = _finding(
        "creator_sell",
        "Creator sold or moved 40% of their tokens (2.0% of supply)",
        MINT,
        CREATOR,
        [helius._encode(bytes([5]) * 64)],
        AT,
    )
    monkeypatch.setattr(memecoins, "creator_sells", lambda rows, balances, **_: ([sell], {}))

    rows, _ = memecoins._collect_helius(
        at=AT,
        rpc=_chain_rpc(_pool_accounts()),
        download=lambda *_: json.dumps({"data": [_gecko_pool()]}).encode(),
    )

    assert rows[0]["early"]["state"] == "avoid"
    assert "Creator is selling" in rows[0]["early"]["reasons"]
    saved = memecoins._market_states(keys=("memecoin_watch_findings",))["memecoin_watch_findings"]
    assert saved[0]["id"] == sell["id"]
