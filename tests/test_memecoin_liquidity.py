from __future__ import annotations

import json

from runner_web.memecoin_liquidity import attach_pools, attach_real_liquidity, quote_side_usd

QUANT = {
    "pairAddress": "E8C4hFkxfyiG8MfrGnoUAK5LWSiPixixws68BeV4ifvF",
    "baseToken": {"address": "wuwWFa5TYNdqceUbEjhecephQz9w8wmijfW1qgencQu"},
    "liquidity": {"usd": 936036353.08, "base": 14604975, "quote": 47.5357},
    "priceNative": "64.09023",
    "priceUsd": "64.090",
}


def test_quant_holds_ninety_five_dollars_not_nine_hundred_million():
    # Live case: QNT/USDC on Raydium CLMM quoted $941M with $47.54 of USDC in it.
    assert round(quote_side_usd(QUANT)) == 95


def test_a_sol_quote_is_priced_in_dollars():
    rug = {"liquidity": {"quote": 0.406}, "priceNative": "0.000001762", "priceUsd": "0.0002093"}
    assert round(quote_side_usd(rug), 2) == round(2 * 0.406 * (0.0002093 / 0.000001762), 2)


def test_chain_rows_keep_theirs_and_others_are_read_from_their_pool():
    chain = {"venue": "pool", "source": "Solana (Helius)", "liquidity_usd": 30_000.0}
    quant = {
        "venue": "pool",
        "source": "GeckoTerminal",
        "liquidity_usd": 941_675_303.0,
        "pool_address": QUANT["pairAddress"],
        "token_address": QUANT["baseToken"]["address"],
    }
    small = {
        "venue": "pool",
        "source": "GeckoTerminal",
        "liquidity_usd": 900.0,
        "pool_address": "Small",
        "token_address": "SmallMint",
    }
    curve = {"venue": "bonding_curve", "liquidity_usd": 5_000.0}
    urls = []

    def download(url, timeout):
        urls.append(url)
        tiny = {
            "pairAddress": "Small",
            "baseToken": {"address": "SmallMint"},
            "liquidity": {"quote": 3.0},
            "priceNative": "0.5",
            "priceUsd": "100",
        }
        return json.dumps({"pairs": [QUANT, tiny]}).encode()

    attach_real_liquidity([chain, quant, small, curve], download=download)

    assert chain["real_liquidity_usd"] == 30_000.0
    assert round(quant["real_liquidity_usd"]) == 95
    # A small pool is read too, so it fails the standard rather than going unchecked.
    assert small["real_liquidity_usd"] == 2 * 3.0 * 200
    assert "real_liquidity_usd" not in curve
    assert urls == [
        "https://api.dexscreener.com/latest/dex/pairs/solana/" + QUANT["pairAddress"] + ",Small"
    ]


def test_a_pair_for_another_coin_or_a_failed_read_leaves_it_unread():
    row = {
        "venue": "pool",
        "source": "GeckoTerminal",
        "liquidity_usd": 50_000.0,
        "pool_address": QUANT["pairAddress"],
        "token_address": "SomeOtherMint",
    }
    attach_real_liquidity(
        [row], download=lambda url, timeout: json.dumps({"pairs": [QUANT]}).encode()
    )
    assert "real_liquidity_usd" not in row

    def down(url, timeout):
        raise OSError("timeout")

    attach_real_liquidity([row], download=down)
    assert "real_liquidity_usd" not in row


def pair(address, base, quote_amount, *, dex="meteora", symbol="SOL", usd=None):
    # SOL at $150: priceUsd / priceNative.
    return {
        "pairAddress": address,
        "dexId": dex,
        "baseToken": {"address": base},
        "quoteToken": {"symbol": symbol},
        "liquidity": {"usd": usd, "quote": quote_amount},
        "priceNative": "0.000001",
        "priceUsd": "0.00015",
    }


def test_every_pool_trading_the_coin_is_listed_busiest_first():
    row = {
        "venue": "pool",
        "token_address": "Coin",
        "pool_address": "Main",
        "real_liquidity_usd": 12_000.0,  # read from the chain; kept over DexScreener's
    }
    urls = []

    def download(url, timeout):
        urls.append(url)
        return json.dumps(
            [
                pair("Main", "Coin", 30.0, dex="pumpswap"),
                pair("Hub", "Coin", 100.0, symbol="RATI", usd=31_000),
                pair("Dust", "Coin", 0.1),
                # The coin as the quote side is another coin's pool, not this one's.
                pair("Other", "Elsewhere", 500.0),
            ]
        ).encode()

    attach_pools([row, {"venue": "bonding_curve", "token_address": "Curve"}], download=download)

    assert urls == ["https://api.dexscreener.com/tokens/v1/solana/Coin"]
    assert [
        (p["address"], p["dex"], p["quote"], p["real_liquidity_usd"]) for p in row["pools"]
    ] == [
        ("Hub", "meteora", "RATI", 2 * 100 * 150),
        ("Main", "pumpswap", "SOL", 12_000.0),
    ]
    assert row["pools"][0]["liquidity_usd"] == 31_000


def test_past_six_pools_the_rest_share_one():
    row = {"venue": "pool", "token_address": "Coin", "pool_address": "P0"}
    pairs = [pair(f"P{i}", "Coin", 10.0 - i) for i in range(9)]

    attach_pools([row], download=lambda url, timeout: json.dumps(pairs).encode())

    assert len(row["pools"]) == 6
    assert row["pools"][-1]["dex"] == "others" and row["pools"][-1]["count"] == 4
    assert row["pools"][-1]["real_liquidity_usd"] == sum(2 * (10.0 - i) * 150 for i in range(5, 9))


def test_a_failed_read_leaves_the_coin_without_a_list():
    row = {"venue": "pool", "token_address": "Coin", "pool_address": "Main"}

    def down(url, timeout):
        raise OSError("offline")

    attach_pools([row], download=down)
    attach_pools([row], download=lambda url, timeout: b'{"error": "rate limited"}')

    assert "pools" not in row
