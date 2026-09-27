from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from runner_web.memecoin_ratify import mint_controls, ratify_rows, standards, top10_share

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


def test_a_coin_meeting_all_seven_standards_is_ratified():
    result = standards(row(), CLEAN, 22.0, False, AT)

    assert result["ratified"] is True
    assert result["met"] == result["total"] == 7


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
    result = standards(row(**changes), controls, top10, bundled, AT)

    assert result["ratified"] is False
    standard = next(item for item in result["standards"] if item["key"] == failing)
    assert standard["met"] is False and standard["detail"] == detail


def test_an_unchecked_standard_is_not_counted_as_met():
    result = standards(row(), None, None, False, AT)

    assert result["ratified"] is False
    unknown = {item["key"] for item in result["standards"] if item["met"] is None}
    assert unknown == {"mint_authority", "freeze_authority", "token_features", "holders"}


class FakeRpc:
    def __init__(self, mints=None, largest=None):
        self.mints = mints or {}
        self.largest = largest or []
        self.calls = []

    def __call__(self, body, *, credits):
        self.calls.append(body["method"])
        if body["method"] == "getMultipleAccounts":
            return {"result": {"value": [self.mints.get(a) for a in body["params"][0]]}}
        if body["method"] == "getTokenLargestAccounts":
            return {"result": {"value": self.largest}}
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
        {"address": VAULT, "uiAmount": 600_000_000},
        {"address": other_pool, "uiAmount": 150_000_000},
    ] + [{"address": f"holder{n}", "uiAmount": 20_000_000} for n in range(12)]
    rpc = FakeRpc(
        mints={
            other_pool: owned_by(bonding_curve(MINT)),
            **{f"holder{n}": owned_by(WALLET) for n in range(12)},
        },
        largest=largest,
    )

    share = top10_share(MINT, supply=1_000_000_000, exclude={VAULT}, rpc=rpc)

    assert share == pytest.approx(20.0)
    assert rpc.calls == ["getTokenLargestAccounts", "getMultipleAccounts"]


def test_only_coins_passing_the_free_standards_cost_a_read():
    rpc = FakeRpc(
        mints={MINT: mint_account(), **{f"holder{n}": owned_by(WALLET) for n in range(10)}},
        largest=[{"address": f"holder{n}", "uiAmount": 10_000_000} for n in range(10)],
    )
    young = row(
        token_address="Young1111111111111111111111111111111111111", pool_created_at=AT.isoformat()
    )
    rows = [row(), young]

    state = ratify_rows(rows, {}, vaults={POOL: VAULT}, bundled=set(), rpc=rpc, at=AT)

    assert rows[0]["ratification"]["ratified"] is True
    assert rows[1]["ratification"]["ratified"] is False
    assert rpc.calls == ["getMultipleAccounts", "getTokenLargestAccounts", "getMultipleAccounts"]
    # Holders are remembered for six hours.
    ratify_rows(
        [row()], state, vaults={POOL: VAULT}, bundled=set(), rpc=rpc, at=AT + timedelta(hours=5)
    )
    assert rpc.calls.count("getTokenLargestAccounts") == 1


def test_the_board_and_page_show_the_mark_and_the_standards():
    from runner_web.market_screens import detail, listing
    from tests.test_market_screens import render, sample

    coin = {**sample("memecoins"), "ratification": standards(row(), CLEAN, 22.0, False, AT)}

    board = render(listing("memecoins", [coin]))
    page = render(detail("memecoins", {"coin": coin, "can_call": True}))

    assert 'class="ratified-mark"' in board
    assert "Ratified · 7 of 7 met" in page
    assert "Top 10 holders own 30% or less · top 10 hold 22%" in page


def test_a_stale_quote_hides_the_mark():
    from runner_web.market_screens import listing
    from tests.test_market_screens import render, sample

    coin = {
        **sample("memecoins"),
        "stale": True,
        "ratification": standards(row(), CLEAN, 22.0, False, AT),
    }

    assert 'class="ratified-mark"' not in render(listing("memecoins", [coin]))


def test_holder_answers_under_older_rules_are_redone():
    rpc = FakeRpc(
        mints={MINT: mint_account(), **{f"holder{n}": owned_by(WALLET) for n in range(10)}},
        largest=[{"address": f"holder{n}", "uiAmount": 10_000_000} for n in range(10)],
    )
    stale = {"holders": {MINT: {"checked_at": AT.isoformat(), "top10_pct": 100.0}}}

    ratify_rows([row()], stale, vaults={}, bundled=set(), rpc=rpc, at=AT)

    assert "getTokenLargestAccounts" in rpc.calls
