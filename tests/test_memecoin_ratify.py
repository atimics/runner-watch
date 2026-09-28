from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from runner_web.memecoin_ratify import mint_controls, ratify_rows, standards, top10_share
from runner_web.solana_keys import PUMP

AT = datetime(2026, 9, 27, 12, tzinfo=UTC)
MINT = "ARPwPPWbaj3FYBf6k1Lt2jqRkv9JJUg3Hxav5aHKTEem"
POOL = "AKKjxxmVZyrBusyPoo11111111111111111111111111"
VAULT = "VauLt111111111111111111111111111111111111111"
CLEAN = {
    "mint_authority": None,
    "freeze_authority": None,
    "risky_extensions": [],
    "supply": 1_000_000_000.0,
    "clean": True,
}


def row(**extra):
    return {
        "token_address": MINT,
        "pool_address": POOL,
        "venue": "pool",
        "liquidity_usd": 25_000.0,
        "pool_created_at": (AT - timedelta(days=2)).isoformat(),
        **extra,
    }


BURNED = {"left_pct": 0.0, "dex": "PumpSwap"}


def full(row_, controls=CLEAN, top10=22.0, bundled=False, **extra):
    facts = {"holder_count": 624, "lock": BURNED, **extra}
    return standards(row_, controls, top10, bundled, AT, **facts)


def test_a_coin_meeting_all_nine_standards_is_ratified():
    result = full(row())

    assert result["ratified"] is True
    assert result["met"] == result["total"] == 9
    details = {item["key"]: item["detail"] for item in result["standards"]}
    assert details["liquidity_lock"] == "all PumpSwap liquidity tokens burned"
    assert details["holder_count"] == "624 holders"


@pytest.mark.parametrize(
    ("facts", "failing", "detail"),
    [
        # Live case: "NPC", 22 holders and $19M of liquidity from one party.
        ({"holder_count": 22}, "holder_count", "22 holders"),
        (
            {"lock": {"left_pct": 100.0, "dex": "Raydium CPMM"}},
            "liquidity_lock",
            "100% of Raydium CPMM liquidity tokens still held",
        ),
    ],
)
def test_the_new_standards_withhold_it(facts, failing, detail):
    result = full(row(), **facts)

    assert result["ratified"] is False
    standard = next(item for item in result["standards"] if item["key"] == failing)
    assert standard["met"] is False and standard["detail"] == detail


def test_a_pool_on_an_unsupported_dex_is_not_checked_yet():
    result = full(row(), lock={"left_pct": None, "dex": None})

    standard = next(item for item in result["standards"] if item["key"] == "liquidity_lock")
    assert result["ratified"] is False
    assert standard["met"] is None and standard["detail"] == "this DEX is not supported yet"


@pytest.mark.parametrize(
    ("changes", "controls", "top10", "bundled", "failing", "detail"),
    [
        (
            {},
            {**CLEAN, "mint_authority": "Auth1", "clean": False},
            22.0,
            False,
            "mint_authority",
            "still active",
        ),
        (
            {},
            {**CLEAN, "freeze_authority": "Auth1", "clean": False},
            22.0,
            False,
            "freeze_authority",
            "still active",
        ),
        (
            {},
            {**CLEAN, "risky_extensions": ["transferHook"], "clean": False},
            22.0,
            False,
            "token_features",
            "transferHook",
        ),
        ({"venue": "bonding_curve"}, CLEAN, 22.0, False, "pool", "on its bonding curve"),
        ({"liquidity_usd": 6_000.0}, CLEAN, 22.0, False, "pool", "$6,000 real liquidity"),
        (
            {"pool_created_at": (AT - timedelta(hours=5)).isoformat()},
            CLEAN,
            22.0,
            False,
            "age",
            "5 hours",
        ),
        ({}, CLEAN, 22.0, True, "record", "launch bundle"),
        (
            {"memecoin_assessment": {"risk": {"factors": [{"kind": "creator_sell"}]}}},
            CLEAN,
            22.0,
            False,
            "record",
            "creator selling",
        ),
        ({}, CLEAN, 45.0, False, "holders", "top 10 hold 45%"),
    ],
)
def test_one_failing_standard_is_enough_to_withhold_it(
    changes, controls, top10, bundled, failing, detail
):
    result = full(row(**changes), controls, top10, bundled)

    assert result["ratified"] is False
    standard = next(item for item in result["standards"] if item["key"] == failing)
    assert standard["met"] is False and standard["detail"] == detail


def test_an_unchecked_standard_is_not_counted_as_met():
    result = standards(row(), None, None, False, AT)

    assert result["ratified"] is False
    unknown = {item["key"] for item in result["standards"] if item["met"] is None}
    assert unknown == {
        "mint_authority",
        "freeze_authority",
        "token_features",
        "holders",
        "holder_count",
        "liquidity_lock",
    }


class FakeRpc:
    def __init__(self, mints=None, largest=None, holders=624):
        self.mints = mints or {}
        self.largest = largest or []
        self.holders = holders
        self.calls = []

    def __call__(self, body, *, credits):
        self.calls.append(body["method"])
        if body["method"] == "getMultipleAccounts":
            return {"result": {"value": [self.mints.get(a) for a in body["params"][0]]}}
        if body["method"] == "getTokenLargestAccounts":
            return {"result": {"value": self.largest}}
        if body["method"] == "getTokenAccounts":
            assert credits == 10 and body["params"]["mint"]
            accounts = [
                {"owner": f"wallet{n}", "amount": 1 if n < self.holders else 0}
                for n in range(min(self.holders + 3, 1000))
            ]
            return {"result": {"token_accounts": accounts}}
        raise AssertionError(body["method"])


def mint_account(*, authority=None, freeze=None, extensions=("metadataPointer", "tokenMetadata")):
    return {
        "data": {
            "parsed": {
                "info": {
                    "mintAuthority": authority,
                    "freezeAuthority": freeze,
                    "supply": str(10**15),
                    "decimals": 6,
                    "extensions": [{"extension": name} for name in extensions],
                }
            }
        }
    }


def test_a_pump_mint_is_clean_and_is_not_read_again():
    rpc = FakeRpc(mints={MINT: mint_account()})

    controls = mint_controls([MINT], {}, rpc=rpc)
    again = mint_controls([MINT], controls, rpc=rpc)

    # Metadata extensions are how Pump names a coin; they are not risky.
    assert controls[MINT]["clean"] is True and controls[MINT]["supply"] == 1_000_000_000
    assert again == controls and rpc.calls == ["getMultipleAccounts"]


def test_a_risky_extension_is_named():
    rpc = FakeRpc(mints={MINT: mint_account(extensions=("transferFeeConfig", "tokenMetadata"))})

    assert mint_controls([MINT], {}, rpc=rpc)[MINT]["risky_extensions"] == ["transferFeeConfig"]


WALLET = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


def owned_by(owner):
    return {"data": {"parsed": {"info": {"owner": owner}}}}


def test_accounts_owned_by_a_program_are_not_counted_among_the_top_holders():
    from runner_web.solana_keys import bonding_curve

    # A pool on another DEX: its token account is owned by a program address.
    other_pool = "OtherDexVau1t11111111111111111111111111111"
    largest = [
        {
            "address": VAULT,
            "amount": str(600_000_000 * 10**6),
            "decimals": 6,
            "uiAmount": None,
        },
        {
            "address": other_pool,
            "amount": str(150_000_000 * 10**6),
            "decimals": 6,
            "uiAmount": None,
        },
    ] + [
        {
            "address": f"holder{n}",
            "amount": str(20_000_000 * 10**6),
            "decimals": 6,
            "uiAmount": None,
        }
        for n in range(12)
    ]
    rpc = FakeRpc(
        mints={
            other_pool: owned_by(bonding_curve(MINT)),
            # The curve account itself belongs to the Pump program.
            bonding_curve(MINT): {"owner": PUMP},
            **{f"holder{n}": owned_by(WALLET) for n in range(12)},
        },
        largest=largest,
    )

    share = top10_share(MINT, supply=1_000_000_000, exclude={VAULT}, rpc=rpc)

    assert share == pytest.approx(20.0)
    assert rpc.calls == ["getTokenLargestAccounts", "getMultipleAccounts", "getMultipleAccounts"]


def test_supply_held_under_an_unknown_program_still_counts():
    from runner_web.solana_keys import find_program_address

    # Live case: "NPC", 22 holders, nearly all supply under program addresses.
    # A program nobody here knows could be the creator's own.
    hidden = find_program_address([b"vault"], WALLET)
    largest = [
        {"address": f"locker{n}", "amount": str(90_000_000 * 10**6), "decimals": 6}
        for n in range(10)
    ]
    rpc = FakeRpc(
        mints={
            **{f"locker{n}": owned_by(hidden) for n in range(10)},
            hidden: {"owner": "UnknownProgram1111111111111111111111111111"},
        },
        largest=largest,
    )

    assert top10_share(MINT, supply=1_000_000_000, exclude=set(), rpc=rpc) == pytest.approx(90.0)


def pump_pool(lp_mint, issued):
    import base64

    from runner_web.helius_discovery import _decode
    from runner_web.memecoin_chain_prices import POOL_DISCRIMINATOR, PUMP_SWAP

    raw = bytearray(260)
    raw[:8] = POOL_DISCRIMINATOR
    raw[107:139] = _decode(lp_mint)
    raw[203:211] = issued.to_bytes(8, "little")
    return {"owner": PUMP_SWAP, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


def lp_mint_account(supply):
    return {"data": {"parsed": {"info": {"supply": str(supply)}}}}


def test_a_graduated_pools_burned_liquidity_is_read_from_its_lp_mint():
    from runner_web.helius_discovery import _encode
    from runner_web.memecoin_ratify import liquidity_locks

    lp = _encode(bytes([33]) * 32)
    # Live shape: P(DOOM)'s pool issued 4,193,388,296,987 and the mint holds 0.
    rpc = FakeRpc(mints={POOL: pump_pool(lp, 4_193_388_296_987), lp: lp_mint_account(0)})

    assert liquidity_locks([POOL], rpc=rpc) == {POOL: {"left_pct": 0.0, "dex": "PumpSwap"}}


def test_only_coins_passing_the_free_standards_cost_a_read():
    from runner_web.helius_discovery import _encode

    lp = _encode(bytes([33]) * 32)
    rpc = FakeRpc(
        mints={
            MINT: mint_account(),
            POOL: pump_pool(lp, 10**12),
            lp: lp_mint_account(0),
            **{f"holder{n}": owned_by(WALLET) for n in range(10)},
        },
        largest=[
            {"address": f"holder{n}", "amount": str(10_000_000 * 10**6), "decimals": 6}
            for n in range(10)
        ],
    )
    young = row(
        token_address="Young1111111111111111111111111111111111111", pool_created_at=AT.isoformat()
    )
    rows = [row(), young]

    state = ratify_rows(rows, {}, vaults={POOL: VAULT}, bundled=set(), rpc=rpc, at=AT)

    assert rows[0]["ratification"]["ratified"] is True
    assert rows[1]["ratification"]["ratified"] is False
    assert rpc.calls.count("getTokenAccounts") == 1
    # Holders and counts are remembered for six hours.
    ratify_rows(
        [row()],
        state,
        vaults={POOL: VAULT},
        bundled=set(),
        rpc=rpc,
        at=AT + timedelta(hours=5),
    )
    assert rpc.calls.count("getTokenLargestAccounts") == 1
    assert rpc.calls.count("getTokenAccounts") == 1


def test_a_failed_holder_count_leaves_it_unknown():
    from runner_web.memecoin_ratify import holder_counts

    def rpc(body, *, credits):
        raise ValueError("Helius request failed")

    assert holder_counts([MINT], {}, rpc=rpc, at=AT) == {}


def test_holders_are_owners_with_a_balance_and_a_full_page_is_a_lower_bound():
    from runner_web.memecoin_ratify import holder_counts

    few = holder_counts([MINT], {}, rpc=FakeRpc(holders=40), at=AT)[MINT]
    many = holder_counts([MINT], {}, rpc=FakeRpc(holders=5000), at=AT)[MINT]

    assert (few["count"], few["at_least"]) == (40, False)
    assert (many["count"], many["at_least"]) == (1000, True)


def test_a_count_from_the_old_geckoterminal_lookup_is_read_again():
    from runner_web.memecoin_ratify import holder_counts

    old = {MINT: {"checked_at": AT.isoformat(), "count": 624}}
    rpc = FakeRpc(holders=300)

    assert holder_counts([MINT], old, rpc=rpc, at=AT)[MINT]["count"] == 300


def test_the_board_and_page_show_the_mark_and_the_standards():
    from runner_web.market_screens import detail, listing
    from tests.test_market_screens import render, sample

    coin = {**sample("memecoins"), "ratification": full(row())}

    board = render(listing("memecoins", [coin]))
    page = render(detail("memecoins", {"coin": coin, "can_call": True}))

    assert 'class="ratified-mark"' in board
    assert "Ratified · 9 of 9 met" in page
    assert "Top 10 holders own 30% or less · top 10 hold 22%" in page


def test_a_stale_quote_hides_the_mark():
    from runner_web.market_screens import listing
    from tests.test_market_screens import render, sample

    coin = {
        **sample("memecoins"),
        "stale": True,
        "ratification": full(row()),
    }

    assert 'class="ratified-mark"' not in render(listing("memecoins", [coin]))


def test_holder_answers_under_older_rules_are_redone():
    rpc = FakeRpc(
        mints={MINT: mint_account(), **{f"holder{n}": owned_by(WALLET) for n in range(10)}},
        largest=[
            {
                "address": f"holder{n}",
                "amount": str(10_000_000 * 10**6),
                "decimals": 6,
                "uiAmount": None,
            }
            for n in range(10)
        ],
    )
    stale = {"holders": {MINT: {"checked_at": AT.isoformat(), "top10_pct": 100.0}}}

    ratify_rows([row()], stale, vaults={}, bundled=set(), rpc=rpc, at=AT)

    assert "getTokenLargestAccounts" in rpc.calls


def test_an_unreadable_owner_counts_as_a_holder():
    largest = [
        {
            "address": f"holder{n}",
            "amount": str(50_000_000 * 10**6),
            "decimals": 6,
            "uiAmount": None,
        }
        for n in range(10)
    ]
    rpc = FakeRpc(mints={}, largest=largest)  # no owner can be read

    share = top10_share(MINT, supply=1_000_000_000, exclude=set(), rpc=rpc)

    assert share == pytest.approx(50.0)


def test_when_the_listed_accounts_are_all_pools_the_hidden_wallets_are_bounded():
    from runner_web.solana_keys import bonding_curve

    # Twenty listed accounts, all program-owned: the wallets are further down,
    # each holding at most the smallest listed balance (2.15%).
    largest = [
        {
            "address": f"pool{n}",
            "amount": str(int(50_000_000 - n * 1_500_000) * 10**6),
            "decimals": 6,
            "uiAmount": None,
        }
        for n in range(20)
    ]
    rpc = FakeRpc(
        mints={
            **{f"pool{n}": owned_by(bonding_curve(MINT)) for n in range(20)},
            bonding_curve(MINT): {"owner": PUMP},
        },
        largest=largest,
    )

    share = top10_share(MINT, supply=1_000_000_000, exclude=set(), rpc=rpc)

    assert share == pytest.approx(10 * 2.15)


def test_a_null_ui_amount_is_not_read_as_zero():
    # Live case: a $19M coin read "top 10 hold 0%" because uiAmount was null.
    largest = [
        {
            "address": f"holder{n}",
            "amount": str(40_000_000 * 10**6),
            "decimals": 6,
            "uiAmount": None,
        }
        for n in range(10)
    ]
    rpc = FakeRpc(mints={f"holder{n}": owned_by(WALLET) for n in range(10)}, largest=largest)

    assert top10_share(MINT, supply=1_000_000_000, exclude=set(), rpc=rpc) == pytest.approx(40.0)
