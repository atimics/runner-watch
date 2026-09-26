from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from runner_web.market_screens import listing, state_tag
from runner_web.memecoin_early import VERSION, early_signal
from runner_web.memecoins import normalize_chain_pools

AT = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def quiet_coin(**extra):
    """An hour trading at its six-hour pace, price flat."""

    return {
        "liquidity_usd": 40_000.0,
        "change_m5": 0.5,
        "change_h1": 2.0,
        "change_h6": 5.0,
        "change_24h": 8.0,
        "volume_m5": 500.0,
        "volume_h1": 6_000.0,
        "volume_h6": 36_000.0,
        "buyers_m5": 4.0,
        "buyers_h1": 40.0,
        "buyers_h6": 240.0,
        "sellers_h1": 40.0,
        "sellers_h6": 240.0,
        **extra,
    }


def waking_coin(**extra):
    """Volume and buyers at four times their earlier pace, price barely moved."""

    return quiet_coin(
        **{
            "volume_h1": 20_000.0,
            "volume_h6": 45_000.0,
            "buyers_h1": 120.0,
            "buyers_h6": 270.0,
            "sellers_h1": 50.0,
            "chain_sentiment_counts": {"bullish": 7, "bearish": 1},
            **extra,
        }
    )


def test_trading_speeding_up_before_the_price_moves_is_a_setup():
    signal = early_signal(waking_coin())

    assert signal["state"] == "setup"
    assert signal["score"] >= 30
    assert "Volume 4.0× its 6h pace" in signal["reasons"]
    assert "Buyers 4.0× their 6h pace" in signal["reasons"]
    assert "71% of traders buying" in signal["reasons"]
    assert "7 unlinked wallets buying" in signal["reasons"]
    assert signal["features"]["organic_buyers"] == 7


def test_steady_trading_gets_no_tag():
    assert early_signal(quiet_coin())["state"] == "quiet"


def test_too_little_trading_to_read_a_pace_is_quiet():
    signal = early_signal(waking_coin(volume_h1=600.0))

    assert signal["state"] == "quiet" and signal["score"] is None


@pytest.mark.parametrize(
    ("moves", "state"),
    [
        ({"change_h1": 60.0}, "running"),
        ({"change_h6": 180.0}, "running"),
        ({"change_h6": 350.0}, "extended"),
        ({"change_24h": 600.0}, "extended"),
    ],
)
def test_a_price_that_already_moved_is_not_a_setup(moves, state):
    assert early_signal(waking_coin(**moves))["state"] == state


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (
            {"memecoin_assessment": {"risk": {"factors": [{"kind": "creator_sell"}]}}},
            "Creator is selling",
        ),
        (
            {"memecoin_assessment": {"risk": {"factors": [{"kind": "synchronized_buys"}]}}},
            "Buying looks staged",
        ),
        ({"liquidity_usd": 2_500.0}, "Pool is too thin"),
        ({"change_24h": -99.9}, "Already collapsed"),
        ({"change_h1": -55.0}, "Dumping this hour"),
    ],
)
def test_a_drained_or_staged_coin_is_avoid_however_busy(extra, reason):
    signal = early_signal(waking_coin(**extra))

    assert signal["state"] == "avoid"
    assert reason in signal["reasons"]


def test_early_states_use_the_stock_tags():
    assert state_tag({"early": {"state": "setup"}}) == ("SETUP", "setup", False)
    assert state_tag({"early": {"state": "running"}}) == ("RUNNING", "running", False)
    assert state_tag({"early": {"state": "extended"}}) == ("EXTENDED", "extended", False)
    assert state_tag({"early": {"state": "avoid"}}) == ("AVOID", "avoid", True)
    assert state_tag({"early": {"state": "quiet"}}) == ("", "", False)
    # A stale quote's reading is not current, so it earns no tag.
    assert state_tag({"early": {"state": "setup"}, "stale": True}) == ("", "", False)


def _board_row(coin_id, **extra):
    return {
        "id": coin_id,
        "symbol": coin_id,
        "name": coin_id,
        "price": 0.001,
        "change_24h": 8.0,
        "volume_24h": 50_000.0,
        "observed_at": AT.isoformat(),
        **extra,
    }


def test_a_setup_leads_the_board_with_its_reasons():
    busy = _board_row("busy", volume_24h=900_000.0, early=early_signal(quiet_coin()))
    setup = _board_row("setup", early=early_signal(waking_coin()))

    rows = listing("memecoins", [busy, setup])["rows"]

    assert [row["id"] for row in rows] == ["setup", "busy"]
    assert rows[0]["tag"] == "SETUP"
    assert rows[0]["rank_detail"].startswith("Volume 4.0× its 6h pace")


def test_pool_quotes_keep_their_short_windows():
    pool = {
        "attributes": {
            "address": "Pool1111111111111111111111111111111111111111",
            "base_token_price_usd": "0.0001",
            "reserve_in_usd": "40000",
            "fdv_usd": "100000",
            "pool_created_at": "2026-09-26T10:00:00Z",
            "price_change_percentage": {"m5": "1.5", "h1": "4", "h6": "9", "h24": "12"},
            "volume_usd": {"m5": "900", "h1": "20000", "h6": "45000", "h24": "80000"},
            "transactions": {
                "m5": {"buys": 9, "sells": 3, "buyers": 8, "sellers": 3},
                "h1": {"buys": 150, "sells": 60, "buyers": 120, "sellers": 50},
                "h6": {"buys": 330, "sells": 250, "buyers": 270, "sellers": 220},
                "h24": {"buys": 900, "sells": 700, "buyers": 600, "sellers": 500},
            },
        },
        "relationships": {
            "network": {"data": {"id": "solana"}},
            "base_token": {"data": {"id": "solana_Mint111111111111111111111111111111111111"}},
        },
    }

    row = normalize_chain_pools({"data": [pool]}, at=AT)[0]

    assert row["change_h1"] == 4.0
    assert row["volume_h1"] == 20_000.0 and row["volume_h6"] == 45_000.0
    assert row["buyers_h1"] == 120.0 and row["sellers_h1"] == 50.0
    assert row["buyers_m5"] == 8.0


def test_each_saved_quote_keeps_its_early_features(tmp_path, monkeypatch):
    from runner_web import db
    from runner_web.memecoin_store import save_memecoin_snapshot

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "early.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    coin = _board_row("setup", early=early_signal(waking_coin()))
    save_memecoin_snapshot([coin], run_id="test", collected_at=AT)

    with db.connection() as database:
        saved = database.execute(
            "SELECT features_json FROM memecoin_quote_history WHERE coin_id='setup'"
        ).fetchone()
    features = json.loads(saved["features_json"])
    assert features["version"] == VERSION
    assert features["state"] == "setup"
    assert features["volume_h1"] == 20_000.0 and features["organic_buyers"] == 7


def test_a_curve_coin_under_an_hour_old_sets_up_on_its_five_minute_pace():
    young = quiet_coin(
        venue="bonding_curve",
        liquidity_usd=3_000.0,
        volume_m5=900.0,
        volume_h1=1_200.0,
        volume_h6=1_200.0,
        buyers_m5=25.0,
        buyers_h1=30.0,
        buyers_h6=30.0,
        sellers_h1=5.0,
    )

    signal = early_signal(young)

    # No earlier hours to compare, so the last five minutes against the hour count.
    assert signal["state"] == "setup"
    assert "Volume 33.0× its 1h pace" in signal["reasons"]
    assert "Buyers 55.0× their 1h pace" in signal["reasons"]
    # A curve always quotes, so its small reserve is not a thin pool.
    assert signal["features"]["on_curve"] is True


def test_a_thin_graduated_pool_is_still_avoid():
    assert early_signal(waking_coin(liquidity_usd=3_000.0))["state"] == "avoid"


def test_a_copy_names_the_original_by_address_on_its_page():
    from runner_web.market_screens import _pool_facts

    original = "DG1Sos2qOriginaL111111111111111111111111111"
    facts = {
        fact["label"]: fact["value"]
        for fact in _pool_facts({"copies": {"token_address": original}})
    }

    assert facts["Original coin"] == original


def _aged(minutes, **extra):
    return quiet_coin(
        pool_created_at=(AT - timedelta(minutes=minutes)).isoformat(),
        observed_at=AT.isoformat(),
        **extra,
    )


def test_a_pool_minutes_old_has_no_pace_to_speed_up_from():
    # Live case: seven minutes after graduating, nearly all of the hour's volume
    # was in the last five minutes, and the old formula read 98,877× its pace.
    signal = early_signal(
        _aged(
            7,
            volume_m5=140_000.0,
            volume_h1=142_404.0,
            volume_h6=142_404.0,
            buyers_m5=300.0,
            buyers_h1=320.0,
            buyers_h6=320.0,
            sellers_h1=70.0,
        )
    )

    assert not any("×" in reason for reason in signal["reasons"])
    assert signal["parts"]["volume_pace"] == signal["parts"]["buyer_pace"] == 0


def test_a_young_pool_is_compared_only_with_the_minutes_it_existed():
    # 20 minutes old: the last 5 minutes against the 15 before them.
    signal = early_signal(
        _aged(
            20,
            volume_m5=900.0,
            volume_h1=1_200.0,
            volume_h6=1_200.0,
            buyers_m5=25.0,
            buyers_h1=30.0,
            buyers_h6=30.0,
            sellers_h1=5.0,
        )
    )

    assert "Volume 9.0× its 1h pace" in signal["reasons"]
    assert "Buyers 15.0× their 1h pace" in signal["reasons"]
