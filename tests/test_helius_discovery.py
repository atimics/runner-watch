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
