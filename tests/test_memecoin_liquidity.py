from __future__ import annotations

import json

from runner_web.memecoin_liquidity import attach_real_liquidity, quote_side_usd

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
        "pool_address": "x",
    }
    curve = {"venue": "bonding_curve", "liquidity_usd": 5_000.0}
    urls = []

    def download(url, timeout):
        urls.append(url)
        return json.dumps({"pairs": [QUANT]}).encode()

    attach_real_liquidity([chain, quant, small, curve], download=download)

    assert chain["real_liquidity_usd"] == 30_000.0
    assert round(quant["real_liquidity_usd"]) == 95
    assert "real_liquidity_usd" not in small and "real_liquidity_usd" not in curve
    assert urls == ["https://api.dexscreener.com/latest/dex/pairs/solana/" + QUANT["pairAddress"]]


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
