from __future__ import annotations

import base64
import struct

import pytest

from runner_web.helius_discovery import _decode, _encode
from runner_web.memecoin_chain_prices import (
    CURVE_DISCRIMINATOR,
    POOL_DISCRIMINATOR,
    SOL,
    SOL_VAULT,
    USDC,
    USDC_VAULT,
    chain_prices,
    mint_supply,
)


def key(number):
    return _encode(bytes([number]) * 32)


MINT, POOL, CURVE = key(1), key(2), key(3)
BASE_VAULT, QUOTE_VAULT = key(4), key(5)


def program_account(raw):
    return {"data": [base64.b64encode(raw).decode(), "base64"]}


def token_account(amount, decimals):
    return {
        "data": {"parsed": {"info": {"tokenAmount": {"amount": str(amount), "decimals": decimals}}}}
    }


def pool_account(*, base_mint=MINT, quote_mint=SOL, virtual=17_584_505_288):
    raw = POOL_DISCRIMINATOR + bytes([255]) + (0).to_bytes(2, "little") + _decode(key(9))
    for address in (base_mint, quote_mint, key(8), BASE_VAULT, QUOTE_VAULT):
        raw += _decode(address)
    raw += (0).to_bytes(8, "little") + _decode(key(9)) + bytes(2)
    raw += virtual.to_bytes(16, "little", signed=True) + bytes(10)
    return program_account(raw)


def curve_account(*, virtual_token, virtual_sol, real_sol, complete=False):
    raw = CURVE_DISCRIMINATOR + struct.pack(
        "<QQQQQ", virtual_token, virtual_sol, 0, real_sol, 10**15
    )
    return program_account(raw + bytes([complete]) + bytes(60))


def fake_rpc(accounts):
    calls = []

    def rpc(body, *, credits):
        calls.append((body["method"], credits, list(body["params"][0])))
        return {"result": {"value": [accounts.get(address) for address in body["params"][0]]}}

    return rpc, calls


def base_accounts(**pool):
    return {
        # 1,000 SOL against 121,290 USDC: SOL at $121.29.
        SOL_VAULT: token_account(1_000 * 10**9, 9),
        USDC_VAULT: token_account(121_290 * 10**6, 6),
        POOL: pool_account(**pool),
        # 800M tokens against 80 SOL, the live 6GgwJp… shape.
        BASE_VAULT: token_account(800_000_000 * 10**6, 6),
        QUOTE_VAULT: token_account(20 * 10**9, 9),
    }


def test_a_pool_price_counts_its_virtual_quote_reserve():
    rpc, calls = fake_rpc(base_accounts())

    result = chain_prices([{"pool_address": POOL, "token_address": MINT}], [], rpc=rpc)

    quote = result["prices"][POOL]
    sol_usd = 121.29
    # (20 real + 17.58 virtual SOL) / 800M tokens, in dollars.
    assert quote["price"] == pytest.approx((20 + 17.584505288) / 800_000_000 * sol_usd)
    # Liquidity is only the money actually in the pool.
    assert quote["liquidity_usd"] == pytest.approx(2 * 20 * sol_usd)
    assert result["sol_usd"] == pytest.approx(sol_usd)
    # One credit a call: pools and the SOL price, then the vaults.
    assert [(method, credits) for method, credits, _ in calls] == [
        ("getMultipleAccounts", 1),
        ("getMultipleAccounts", 1),
    ]


def test_a_usdc_pool_is_priced_in_dollars_directly():
    accounts = base_accounts(quote_mint=USDC, virtual=2_516_200_564)
    accounts[QUOTE_VAULT] = token_account(5_000 * 10**6, 6)
    rpc, _ = fake_rpc(accounts)

    quote = chain_prices([{"pool_address": POOL, "token_address": MINT}], [], rpc=rpc)["prices"][
        POOL
    ]

    assert quote["price"] == pytest.approx((5_000 + 2_516.200564) / 800_000_000)


def test_a_pool_for_another_mint_is_left_to_geckoterminal():
    rpc, _ = fake_rpc(base_accounts(base_mint=key(7)))

    prices = chain_prices([{"pool_address": POOL, "token_address": MINT}], [], rpc=rpc)["prices"]

    assert prices == {}


def test_a_curve_price_is_its_virtual_reserves():
    accounts = base_accounts()
    accounts[CURVE] = curve_account(
        virtual_token=1_000_000_000 * 10**6, virtual_sol=30 * 10**9, real_sol=2 * 10**9
    )
    rpc, _ = fake_rpc(accounts)

    quote = chain_prices([], [{"pool_address": CURVE}], rpc=rpc)["prices"][CURVE]

    assert quote["price"] == pytest.approx(30 / 1_000_000_000 * 121.29)
    assert quote["liquidity_usd"] == pytest.approx(2 * 121.29)


def test_a_completed_curve_has_graduated_and_is_not_priced():
    accounts = base_accounts()
    accounts[CURVE] = curve_account(
        virtual_token=10**15, virtual_sol=10**11, real_sol=85 * 10**9, complete=True
    )
    rpc, _ = fake_rpc(accounts)

    assert chain_prices([], [{"pool_address": CURVE}], rpc=rpc)["prices"] == {}


def test_no_sol_price_means_no_chain_prices():
    accounts = base_accounts()
    accounts[SOL_VAULT] = None
    rpc, _ = fake_rpc(accounts)

    with pytest.raises(ValueError):
        chain_prices([{"pool_address": POOL, "token_address": MINT}], [], rpc=rpc)


def mint_account(supply, decimals):
    return {"data": {"parsed": {"info": {"supply": str(supply), "decimals": decimals}}}}


def test_supply_is_read_with_the_vaults_when_asked():
    accounts = base_accounts()
    accounts[MINT] = mint_account(1_000_000_000 * 10**6, 6)
    rpc, calls = fake_rpc(accounts)
    pool = {"pool_address": POOL, "token_address": MINT}

    quote = chain_prices([pool], [], rpc=rpc, supply=True)["prices"][POOL]

    assert quote["supply"] == 1_000_000_000
    # The mint rides in the vault call: still two calls.
    assert len(calls) == 2 and MINT in calls[1][2]
    assert "supply" not in chain_prices([pool], [], rpc=fake_rpc(accounts)[0])["prices"][POOL]


def test_a_curve_supply_is_read_too():
    accounts = base_accounts()
    accounts[CURVE] = curve_account(
        virtual_token=1_000_000_000 * 10**6, virtual_sol=30 * 10**9, real_sol=2 * 10**9
    )
    accounts[MINT] = mint_account(1_000_000_000 * 10**6, 6)
    rpc, _ = fake_rpc(accounts)

    prices = chain_prices(
        [], [{"pool_address": CURVE, "token_address": MINT}], rpc=rpc, supply=True
    )

    assert prices["prices"][CURVE]["supply"] == 1_000_000_000


def test_an_unreadable_mint_has_no_supply():
    assert mint_supply(None) is None
    assert mint_supply(mint_account(0, 6)) is None
