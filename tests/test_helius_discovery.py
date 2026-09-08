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
