from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from runner_web import db
from runner_web import main as web_main
from runner_web import onchain_wallets as wallets
from runner_web.helius_discovery import PUMP_SWAP, _encode
from runner_web.memecoin_chain_parser import BUY, SELL, SOL, USDC
from runner_web.solana_keys import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from runner_web.wallet_registry import register_chain, wallet

AT = datetime(2026, 10, 2, 17, tzinfo=UTC)
ADDRESS = _encode(bytes([3]) * 32)
MINT = _encode(bytes([4]) * 32)
POOL = _encode(bytes([5]) * 32)
QUOTES = {
    SOL: {"symbol": "SOL", "price_usd": "100"},
    USDC: {"symbol": "USDC", "price_usd": "1"},
    MINT: {"symbol": "TEST", "price_usd": "4"},
}


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "wallet.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    web_main.RATE_LIMITS.clear()
    db.init_db()


def token_balance(mint, amount):
    return {"mint": mint, "owner": ADDRESS,
            "uiTokenAmount": {"amount": str(amount), "decimals": 0}}


def transaction(index, before, after, cash, *, quote=SOL, transfer=False, failed=False):
    pre, post = [token_balance(MINT, before)], [token_balance(MINT, after)]
    if quote == USDC:
        pre.append(token_balance(USDC, 100))
        post.append(token_balance(USDC, 100 + cash))
    instruction = {
        "programId": PUMP_SWAP, "accounts": [POOL, ADDRESS, POOL, MINT],
        "data": _encode((BUY if after > before else SELL) + bytes(16)),
    }
    return {
        "slot": 100 + index, "transactionIndex": index,
        "blockTime": int(AT.timestamp()) - 300 + index,
        "transaction": {
            "signatures": [_encode(bytes([index + 1]) * 64)],
            "message": {"accountKeys": [{"pubkey": ADDRESS}],
                        "instructions": [] if transfer else [instruction]},
        },
        "meta": {
            "err": {"InstructionError": [0, "test"]} if failed else None, "fee": 5000,
            "preBalances": [10_000_000_000],
            "postBalances": [10_000_000_000 + int(cash * 10**9) if quote == SOL
                             else 9_999_995_000],
            "preTokenBalances": pre, "postTokenBalances": post, "innerInstructions": [],
        },
    }


def sample_pnl():
    entries = [
        transaction(1, 0, 100, -2),
        transaction(2, 100, 200, -4),
        transaction(3, 200, 150, 2),
    ]
    return wallets.calculate_pnl(ADDRESS, list(reversed(entries)), {MINT: Decimal(150)}, QUOTES)


def test_weighted_cost_partial_sell_and_current_mark():
    result = sample_pnl()
    assert result["pnl"][0]["realized"] == 0.5
    assert result["pnl"][0]["unrealized"] == 1.5
    assert result["holdings"][0]["cost"] == 4.5
    assert result["holdings"][0]["value_usd"] == 600
    assert result["fees_sol"] == pytest.approx(0.000015)
    assert result["unknown_sales"] == 0
    assert result["trades"][0]["side"] == "Sell"
    assert len(result["receipts"][0]["hash"]) == 64


def test_transfer_and_missing_opening_cost_stay_unknown():
    entries = [transaction(1, 0, 100, 0, transfer=True),
               transaction(2, 100, 50, 2)]
    result = wallets.calculate_pnl(ADDRESS, entries, {MINT: Decimal(50)}, QUOTES)
    assert result["unknown_sales"] == 1
    assert result["unknown_holdings"] == 1
    assert result["trades"][0]["realized"] is None
    assert result["holdings"][0]["cost"] is None
    opening = wallets.calculate_pnl(
        ADDRESS, [transaction(1, 100, 50, 2)], {MINT: Decimal(50)}, QUOTES
    )
    assert opening["unknown_sales"] == 1


def test_outbound_transfer_reduces_cost_without_realizing_profit():
    entries = [transaction(1, 0, 100, -2),
               transaction(2, 100, 50, 0, transfer=True),
               transaction(3, 50, 0, 2)]
    result = wallets.calculate_pnl(ADDRESS, entries, {}, QUOTES)
    assert result["pnl"][0]["realized"] == 1
    assert len(result["trades"]) == 2


def test_failed_trade_preserves_inventory_and_paid_fees():
    failed = transaction(2, 100, 0, 2, failed=True)
    result = wallets.calculate_pnl(
        ADDRESS, [transaction(1, 0, 100, -2), failed], {MINT: Decimal(100)}, QUOTES
    )
    assert result["holdings"][0]["cost"] == 2
    assert len(result["trades"]) == 1
    assert result["receipts"][0]["failed"]
    assert result["fees_sol"] == pytest.approx(0.00001)


def test_usdc_results_keep_their_own_unit():
    entries = [transaction(1, 0, 100, -40, quote=USDC),
               transaction(2, 100, 50, 30, quote=USDC)]
    result = wallets.calculate_pnl(ADDRESS, entries, {MINT: Decimal(50)}, QUOTES)
    assert result["pnl"][0]["realized"] == 0
    assert result["pnl"][1]["realized"] == 10
    assert result["pnl"][1]["unrealized"] == 180


def test_wrapped_sol_and_native_sol_are_one_quote():
    entry = transaction(1, 0, 100, -3)
    entry["meta"]["preTokenBalances"].append(token_balance(SOL, 0))
    entry["meta"]["postTokenBalances"].append(token_balance(SOL, 1))
    result = wallets.calculate_pnl(ADDRESS, [entry], {MINT: Decimal(100)}, QUOTES)
    assert result["holdings"][0]["cost"] == 2


def test_missing_prices_and_balance_mismatch_keep_open_pnl_pending():
    entry = transaction(1, 0, 100, -2)
    unpriced = wallets.calculate_pnl(ADDRESS, [entry], {MINT: Decimal(100)}, {})
    assert unpriced["holdings"][0]["value_usd"] is None
    assert unpriced["pnl"][0]["unrealized"] is None
    mismatch = wallets.calculate_pnl(ADDRESS, [entry], {MINT: Decimal(101)}, QUOTES)
    assert mismatch["holdings"][0]["cost"] is None


def test_duplicates_and_same_slot_order():
    first, second = transaction(1, 0, 100, -2), transaction(2, 100, 0, 3)
    second["slot"] = first["slot"]
    result = wallets.calculate_pnl(ADDRESS, [second, first, copy.deepcopy(first)], {}, QUOTES)
    assert result["transactions"] == 2
    assert result["pnl"][0]["realized"] == 1
    del first["transactionIndex"]
    del second["transactionIndex"]
    result = wallets.calculate_pnl(ADDRESS, [second, first], {}, QUOTES)
    assert result["pnl"][0]["realized"] == 1


def test_bad_owner_and_multitoken_swaps_require_cost_evidence():
    bad = transaction(1, 0, 100, -2)
    del bad["meta"]["preTokenBalances"][0]["owner"]
    result = wallets.calculate_pnl(ADDRESS, [bad], {MINT: Decimal(100)}, QUOTES)
    assert result["excluded_transactions"] == 1
    assert result["holdings"][0]["cost"] is None
    multi = transaction(1, 0, 100, -2)
    other = _encode(bytes([6]) * 32)
    multi["meta"]["preTokenBalances"].append(token_balance(other, 10))
    multi["meta"]["postTokenBalances"].append(token_balance(other, 0))
    result = wallets.calculate_pnl(ADDRESS, [multi], {MINT: Decimal(100)}, QUOTES)
    assert result["holdings"][0]["cost"] is None


def fake_rpc(seen):
    def call(body, *, credits):
        seen.append((body, credits))
        method = body["method"]
        if method == "getTransactionsForAddress":
            return {"result": {"data": [transaction(1, 0, 100, -2)]}}
        if method == "getBalance":
            return {"result": {"context": {"slot": 110}, "value": 8_000_000_000}}
        if method == "getTokenAccountsByOwner":
            program = body["params"][1]["programId"]
            rows = [{"account": {"data": {"parsed": {"info": {
                "mint": MINT, "owner": ADDRESS, "tokenAmount": {"amount": "100", "decimals": 0}
            }}}}}] if program == TOKEN_PROGRAM else []
            return {"result": {"value": rows}}
        raise AssertionError(method)
    return call


def download(*_):
    return json.dumps({"data": [
        {"id": "solana_" + mint, "attributes": fields} for mint, fields in QUOTES.items()
    ]}).encode()


def test_collector_uses_finalized_history_both_token_programs_and_bounded_prices():
    seen = []
    result = wallets.collect_wallet(ADDRESS, at=AT, rpc=fake_rpc(seen), download=download)
    assert result["holdings"][0]["cost"] == 2
    assert result["balance_sol"] == 8
    assert sum(credits for _, credits in seen) == 13
    request = seen[0][0]["params"][1]
    assert request["filters"]["tokenAccounts"] == "balanceChanged"
    assert request["filters"]["status"] == "any"
    assert request["commitment"] == "finalized"
    assert request["filters"]["blockTime"]["lte"] == int(AT.timestamp())
    assert seen[-1][0]["params"][1]["programId"] == TOKEN_2022_PROGRAM


def test_paging_continues_on_partial_pages_and_stops_at_bound():
    seen = []
    original = fake_rpc(seen)
    def rpc(body, *, credits):
        if body["method"] == "getTransactionsForAddress":
            seen.append((body, credits))
            cursor = body["params"][1].get("paginationToken")
            return {"result": {"data": [transaction(1 if not cursor else 2, 0, 100, -2)],
                               "paginationToken": "100:1" if not cursor else "90:1"}}
        return original(body, credits=credits)
    result = wallets.collect_wallet(ADDRESS, at=AT, rpc=rpc, download=download)
    assert result["has_more"]
    assert len([body for body, _ in seen if body["method"] == "getTransactionsForAddress"]) == 2


def test_refresh_is_durable_and_shares_cooldown(database):
    seen = []
    first = wallets.refresh_wallet(ADDRESS, at=AT, rpc=fake_rpc(seen), download=download)
    second = wallets.refresh_wallet(
        ADDRESS, at=AT + timedelta(seconds=1), rpc=fake_rpc(seen), download=download
    )
    assert first["status"] == second["status"] == "ready"
    assert len(seen) == 4
    assert second["history_hash"] == first["history_hash"]
    assert wallets.saved_wallet(ADDRESS, at=AT + timedelta(minutes=20))["status"] == "stale"


def test_concurrent_refreshes_claim_one_paid_read(database, monkeypatch):
    seen = []
    original = wallets.collect_wallet
    def collect(address, **kwargs):
        seen.append(address)
        return original(address, **kwargs)
    monkeypatch.setattr(wallets, "collect_wallet", collect)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: wallets.refresh_wallet(
            ADDRESS, at=AT, rpc=fake_rpc([]), download=download
        ), range(4)))
    assert len(seen) == 1


def test_failure_keeps_saved_data_and_hides_provider_keys(database):
    wallets.refresh_wallet(ADDRESS, at=AT, rpc=fake_rpc([]), download=download)
    def fail(*_, **__):
        raise ValueError("https://private-provider/?api-key=secret")
    result = wallets.refresh_wallet(
        ADDRESS, at=AT + timedelta(minutes=20), rpc=fail, download=download
    )
    assert result["status"] == "stale"
    assert result["holdings"][0]["cost"] == 2
    assert "secret" not in json.dumps(result)
    assert result["error"]


def test_catalog_has_52_valid_unique_addresses_and_source_dates(database):
    rows = wallets.wallet_catalog()["wallets"]
    assert len(rows) == len({row["address"] for row in rows}) == 52
    assert all(wallet(row["id"])["kind"] == "solana" for row in rows)
    assert rows[0]["label_date"] == "2025-01-26"
    assert "2712bbd" in rows[0]["source_url"]


def test_wallet_routes_open_seeds_arbitrary_addresses_and_saved_pnl(database, monkeypatch):
    client = TestClient(web_main.app)
    catalogue = client.get("/wallets")
    assert catalogue.status_code == 200
    assert "Profit" in catalogue.text
    opened = client.get("/wallets", params={"q": ADDRESS}, follow_redirects=False)
    wallet_id = register_chain(ADDRESS)
    assert opened.headers["location"] == "/wallet/" + wallet_id
    assert wallet(wallet_id)["address"] == ADDRESS
    wallets.refresh_wallet(ADDRESS, at=datetime.now(UTC), rpc=fake_rpc([]), download=download)
    page = client.get("/wallet/" + wallet_id)
    assert page.status_code == 200
    assert "History and cost coverage" in page.text
    assert "Open gains" in page.text
    assert "TEST" in page.text
    assert client.get("/api/wallets/" + wallet_id + "/pnl").json()["balance_sol"] == 8
    assert client.post("/api/wallets/" + wallet_id + "/refresh").status_code == 403
    assert client.get("/wallets/solana/invalid").status_code == 400
    assert client.get("/api/wallets/w_0000000000000000/pnl").status_code == 404


def test_pending_page_keeps_numbers_pending(database):
    client = TestClient(web_main.app)
    wallet_id = register_chain(ADDRESS)
    page = client.get("/wallet/" + wallet_id)
    assert page.status_code == 200
    assert "waiting for its first read" in page.text
    assert "0.0000 SOL" not in page.text
