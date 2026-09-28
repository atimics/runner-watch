from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError

from runner_web.memecoin_copycats import (
    MIN_COPIES,
    copycat_bursts,
    find_originals,
    mark_copycats,
    name_key,
)
from runner_web.memecoin_early import early_signal

AT = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
# Shaped on the live "TROLLOWEEN" search of 2026-09-26.
ORIGINAL = "DG1Sos2qOriginaL111111111111111111111111111"
OLD_COPY = "69N57UEDo1dCopy11111111111111111111111111111"
NEW_COPY = "ZrQdfsbmNewCopy1111111111111111111111111111"


def launch(mint, name, *, minutes_ago=30, symbol=None):
    return {
        "kind": "token_launch",
        "token_address": mint,
        "claimed_name": name,
        "claimed_symbol": symbol or name.upper(),
        "observed_at": (AT - timedelta(minutes=minutes_ago)).isoformat(),
        "signature": "sig-" + mint[:6],
        "source_url": "https://solscan.io/tx/sig-" + mint[:6],
    }


def burst_launches():
    return [
        launch(NEW_COPY, "TROLLOWEEN", minutes_ago=20),
        launch("Copy2" + "1" * 38, "Tr0lloween", minutes_ago=90),
        launch("Copy3" + "1" * 38, "tro11oween", minutes_ago=300),
    ]


def search_pool(mint, address, created, liquidity, volume):
    return {
        "attributes": {
            "address": address,
            "name": "TROLLOWEEN / SOL",
            "pool_created_at": created,
            "reserve_in_usd": str(liquidity),
            "volume_usd": {"h24": str(volume)},
        },
        "relationships": {"base_token": {"data": {"id": "solana_" + mint}}},
    }


def search_body():
    token = {"type": "token", "attributes": {"name": "TROLLOWEEN", "symbol": "TROLLOWEEN"}}
    return {
        "data": [
            search_pool(NEW_COPY, "NewPool", "2026-10-30T15:00:00Z", 27_352, 209_147),
            search_pool(ORIGINAL, "OgPumpswap", "2025-10-07T12:00:00Z", 13_222, 7_200),
            search_pool(ORIGINAL, "OgMeteora", "2025-10-09T12:00:00Z", 875, 635),
            search_pool(OLD_COPY, "OldCopyPool", "2025-10-02T12:00:00Z", 4_499, 18),
        ],
        "included": [
            {**token, "id": "solana_" + NEW_COPY},
            {**token, "id": "solana_" + ORIGINAL},
            {**token, "id": "solana_" + OLD_COPY, "attributes": {"name": "Trolloween"}},
        ],
    }


def test_look_alike_names_fold_together():
    keys = {name_key(text) for text in ("Trolloween", "TR0LLOWEEN", "tro11oween", "$Trolloween")}
    assert len(keys) == 1
    assert name_key("Ｔｒｏｌｌｏｗｅｅｎ") in keys
    assert name_key("ab") == ""


def test_three_launches_taking_one_name_in_a_day_are_a_burst():
    too_old = launch("Old" + "1" * 40, "Trolloween", minutes_ago=2000)
    bursts = copycat_bursts(burst_launches() + [too_old], AT)

    burst = bursts[name_key("Trolloween")]
    assert burst["h24"] == MIN_COPIES
    assert burst["h1"] == 1
    assert NEW_COPY in burst["launches"]


def test_two_launches_are_not_a_burst():
    assert copycat_bursts(burst_launches()[:2], AT) == {}


def test_the_original_is_the_best_funded_coin_a_week_older_than_the_copies():
    bursts = copycat_bursts(burst_launches(), AT)
    calls = []

    def download(url, timeout):
        calls.append(url)
        return json.dumps(search_body()).encode()

    originals, cache, searches = find_originals(bursts, {}, download=download, at=AT, pause=0)

    original = originals[name_key("Trolloween")]
    # Not today's copy, and not the older copy that died with $4.5K.
    assert original["token_address"] == ORIGINAL
    assert original["pool_address"] == "OgPumpswap"
    assert original["liquidity_usd"] == 13_222 + 875
    assert searches == 1 and "include=base_token" in calls[0]

    # The answer is cached, so the next cycle does not search again.
    again, _, searches = find_originals(bursts, cache, download=download, at=AT, pause=0)
    assert searches == 0 and again == originals


def test_no_original_when_nothing_is_old_and_funded_enough():
    body = search_body()
    body["data"] = [pool for pool in body["data"] if pool["attributes"]["address"] == "OldCopyPool"]
    bursts = copycat_bursts(burst_launches(), AT)

    originals, _, _ = find_originals(
        bursts, {}, download=lambda *_: json.dumps(body).encode(), at=AT, pause=0
    )

    assert originals == {}


def test_a_failed_search_skips_the_name_without_failing():
    def download(url, timeout):
        raise HTTPError(url, 429, "Too Many Requests", {}, None)

    originals, cache, searches = find_originals(
        copycat_bursts(burst_launches(), AT), {}, download=download, at=AT, pause=0
    )

    assert originals == {} and cache == {} and searches == 1


def test_the_original_gets_watch_on_its_copies_and_each_copy_is_avoid():
    bursts = copycat_bursts(burst_launches(), AT)
    originals, _, _ = find_originals(
        bursts, {}, download=lambda *_: json.dumps(search_body()).encode(), at=AT, pause=0
    )
    quiet = {"liquidity_usd": 14_000.0, "volume_h1": 200.0, "buyers_h1": 3.0, "change_h1": 1.0}
    rows = [{"token_address": ORIGINAL, **quiet}, {"token_address": NEW_COPY, **quiet}]

    mark_copycats(rows, bursts, originals)
    original, copy = (early_signal(row) for row in rows)

    # Its own trading is still quiet; the copies are what point here.
    assert original["state"] == "watch"
    assert original["reasons"][0] == "3 copycat launches in 24h, 1 in the last hour"
    assert rows[0]["copycats"]["evidence"][0]["source_url"].startswith("https://solscan.io/tx/")
    assert copy["state"] == "avoid"
    assert copy["reasons"][0] == "Copies an older coin's name"
    assert rows[1]["copies"] == {"token_address": ORIGINAL}


def _originals_for(body):
    bursts = copycat_bursts(burst_launches(), AT)
    originals, _, _ = find_originals(
        bursts, {}, download=lambda *_: json.dumps(body).encode(), at=AT, pause=0
    )
    return originals


def test_a_matching_symbol_alone_does_not_make_an_original():
    # Live case: "Hold My Glasses" (symbol GLASSES) answered a "Glasses" burst.
    body = search_body()
    for token in body["included"]:
        if token["id"] == "solana_" + ORIGINAL:
            token["attributes"] = {"name": "Hold My Trolloween", "symbol": "TROLLOWEEN"}

    assert _originals_for(body) == {}


def test_a_coin_that_no_longer_trades_is_not_an_original():
    body = search_body()
    for pool in body["data"]:
        pool["attributes"]["volume_usd"] = {"h24": "0"}

    assert _originals_for(body) == {}


def test_answers_cached_under_older_rules_are_searched_again():
    bursts = copycat_bursts(burst_launches(), AT)
    stale = {
        name_key("Trolloween"): {
            "checked_at": AT.isoformat(),
            "original": {"token_address": "WrongOldAnswer"},
        }
    }

    originals, _, searches = find_originals(
        bursts,
        stale,
        download=lambda *_: json.dumps(search_body()).encode(),
        at=AT,
        pause=0,
    )

    assert searches == 1
    assert originals[name_key("Trolloween")]["token_address"] == ORIGINAL
