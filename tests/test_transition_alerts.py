from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from runner_web import db, telegram_outbox, transition_alerts
from runner_web.caller_ids import ensure_caller_identity_with_database
from runner_web.telegram import TelegramConfig, format_transition_post_md, next_segment
from runner_web.transition_alerts import classify, detect, interest, queue_transitions

AT = datetime(2026, 9, 30, 15, tzinfo=UTC)
CONFIG = TelegramConfig("fixture-token", "fixture-chat")
MINT = "So11111111111111111111111111111111111111112"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "transitions.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setenv("TELEGRAM_CHANNEL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("TELEGRAM_TICKER_QUIET_SECONDS", "1800")
    monkeypatch.delenv("TELEGRAM_MEMECOIN_ALERTS", raising=False)
    db.init_db()


def stock(ticker="SOUN", tag="setup", ratified=True, **extra):
    return {
        "market": "stock",
        "subject": ticker,
        "ticker": ticker,
        "tag": tag,
        "ratified": ratified,
        "met": 9,
        "total": 9,
        "score": 70,
        "relative_volume": 4.2,
        **extra,
    }


def run_detect(database, rows, at):
    return detect(database, "stock", {row["subject"]: row for row in rows}, at)


def events(database, status=None):
    sql = "SELECT key,event,score,status FROM transition_events"
    if status:
        return database.execute(sql + " WHERE status=?", (status,)).fetchall()
    return database.execute(sql).fetchall()


@pytest.mark.parametrize(
    "before,before_ratified,now,ratified,call,expected",
    [
        ("watch", True, "setup", True, False, "setup"),
        ("setup", True, "running", True, False, "running"),
        ("watch", True, "running", True, False, "running"),
        ("running", True, "extended", True, False, "extended"),
        ("setup", True, "avoid", True, False, "avoid"),
        ("setup", False, "avoid", False, True, "avoid"),
        ("setup", True, "avoid", False, False, "lost_ratification"),
        ("setup", True, "avoid", True, False, "avoid"),
        ("setup", False, "setup", True, False, "newly_ratified"),
        ("setup", True, "setup", False, False, "lost_ratification"),
        # A ratification change beats the tag move beside it only when heavier.
        ("setup", False, "running", True, False, "running"),
        ("watch", False, "setup", True, False, "newly_ratified"),
        # Silent.
        ("avoid", True, "setup", True, False, None),
        ("avoid", True, "running", True, False, None),
        ("extended", True, "running", True, False, None),
        ("setup", True, "setup", True, False, None),
        ("setup", False, "running", False, False, None),
        ("setup", None, "running", None, False, None),
        ("setup", None, "setup", True, False, None),
        ("paused", True, "setup", True, False, None),
        ("setup", True, "paused", True, False, None),
        ("watch", False, "avoid", False, False, None),
        ("setup", True, "extended", True, False, None),
    ],
)
def test_classify(before, before_ratified, now, ratified, call, expected):
    found = classify(before, before_ratified, now, ratified, call)
    assert (found[0] if found else None) == expected


def test_a_call_lets_an_unratified_name_speak_about_its_tag():
    assert classify("setup", False, "running", False, True)[0] == "running"


def test_a_quiet_coin_reads_as_watch():
    assert classify("quiet", True, "setup", True, False)[0] == "setup"
    assert classify("", True, "setup", True, False)[0] == "setup"


def test_interest_orders_events_then_adds_evidence():
    facts = {"market": "stock", "met": 9, "total": 9, "score": 50, "relative_volume": 2}
    order = ["running", "newly_ratified", "setup", "extended", "lost_ratification", "avoid"]
    scores = [interest(event, facts, False) for event in order]
    assert scores == sorted(scores, reverse=True)
    assert interest("setup", facts, True) == interest("setup", facts, False) + 25


def test_interest_rewards_volume_and_liquidity():
    quiet = {"market": "stock", "met": 9, "total": 9, "score": 50, "relative_volume": 1}
    loud = {**quiet, "relative_volume": 9}
    assert interest("setup", loud, False) > interest("setup", quiet, False)
    thin = {"market": "memecoin", "met": 3, "total": 4, "liquidity_usd": 6_000}
    deep = {**thin, "liquidity_usd": 900_000, "volume_24h": 4_000_000}
    assert interest("setup", deep, False) > interest("setup", thin, False)
    assert interest("setup", {"market": "memecoin"}, False) == 60.0


def test_a_first_sighting_says_nothing_then_a_change_is_stored():
    with db.connection() as database:
        assert run_detect(database, [stock(tag="setup")], AT) == 0
        assert run_detect(database, [stock(tag="setup")], AT + timedelta(minutes=5)) == 0
        assert run_detect(database, [stock(tag="running")], AT + timedelta(minutes=10)) == 1
        rows = events(database)
    assert [(row["event"], row["status"]) for row in rows] == [("running", "pending")]
    assert rows[0]["key"] == "transition:SOUN:setup>running:2026-09-30"


def test_an_unratified_name_with_no_call_never_alerts():
    with db.connection() as database:
        run_detect(database, [stock(tag="setup", ratified=False)], AT)
        assert run_detect(database, [stock(tag="running", ratified=False)], AT) == 0


def test_a_flapping_name_speaks_once_a_day():
    with db.connection() as database:
        run_detect(database, [stock(tag="setup")], AT)
        assert run_detect(database, [stock(tag="running")], AT + timedelta(minutes=5)) == 1
        run_detect(database, [stock(tag="setup")], AT + timedelta(minutes=10))
        assert run_detect(database, [stock(tag="running")], AT + timedelta(minutes=15)) == 0
        assert len(events(database)) == 1


def test_a_name_last_seen_long_ago_is_a_first_sighting_again():
    with db.connection() as database:
        run_detect(database, [stock(tag="setup")], AT)
        assert run_detect(database, [stock(tag="running")], AT + timedelta(days=2)) == 0


def test_a_failed_ratification_read_is_not_a_loss():
    with db.connection() as database:
        run_detect(database, [stock(ratified=True)], AT)
        assert run_detect(database, [stock(ratified=None)], AT + timedelta(minutes=5)) == 0
        assert run_detect(database, [stock(ratified=False)], AT + timedelta(minutes=10)) == 1
        assert events(database)[0]["event"] == "lost_ratification"


def test_an_active_call_counts_for_a_stock():
    with db.connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("owner", "owner", "Owner", "active", AT.isoformat()),
        )
        identity = ensure_caller_identity_with_database(database, "owner")
        database.execute(
            """
            INSERT INTO community_calls(
                id,public_id,user_id,caller_identity_id,ticker,side,entry_price,entry_at,
                status,created_at,updated_at
            ) VALUES('c1','c1','owner',?,'SOUN','long',1,?,'active',?,?)
            """,
            (identity["id"], AT.isoformat(), AT.isoformat(), AT.isoformat()),
        )
        run_detect(database, [stock(tag="setup", ratified=False)], AT)
        assert run_detect(database, [stock(tag="running", ratified=False)], AT) == 1
        assert events(database)[0]["score"] > 100


def _patch_board(monkeypatch, board):
    monkeypatch.setattr(transition_alerts, "_stock_observations", lambda *_: board["rows"])


def board_of(*rows):
    return {"rows": {row["subject"]: row for row in rows}}


def test_the_best_pending_event_goes_out_one_at_a_time(monkeypatch):
    board = board_of(stock("AAAA", "setup"), stock("BBBB", "setup", relative_volume=9.0))
    _patch_board(monkeypatch, board)
    with db.connection() as database:
        assert queue_transitions(database, CONFIG, origin="https://app.test", at=AT) == 0
    board["rows"] = {
        "AAAA": stock("AAAA", "running", relative_volume=1.0),
        "BBBB": stock("BBBB", "running", relative_volume=9.0),
    }
    later = AT + timedelta(minutes=5)
    with db.connection() as database:
        assert queue_transitions(database, CONFIG, origin="https://app.test", at=later) == 1
        pending = database.execute(
            "SELECT subject FROM transition_events WHERE status='pending'"
        ).fetchall()
        first = database.execute("SELECT text FROM telegram_outbox").fetchall()
        # One transition waits at a time.
        assert queue_transitions(database, CONFIG, origin="https://app.test", at=later) == 0
    assert [row["subject"] for row in pending] == ["AAAA"]
    assert "BBBB" in first[0]["text"]


def test_daily_cap_holds_back_the_rest(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TRANSITIONS_PER_DAY_STOCK", "1")
    monkeypatch.setenv("TELEGRAM_TICKER_QUIET_SECONDS", "0")
    board = board_of(stock("AAAA", "setup"), stock("BBBB", "setup"))
    _patch_board(monkeypatch, board)
    with db.connection() as database:
        queue_transitions(database, CONFIG, origin="https://app.test", at=AT)
    board["rows"] = {"AAAA": stock("AAAA", "running"), "BBBB": stock("BBBB", "running")}
    at = AT + timedelta(minutes=5)
    with db.connection() as database:
        assert queue_transitions(database, CONFIG, origin="https://app.test", at=at) == 1
        database.execute("UPDATE telegram_outbox SET status='sent'")
        assert queue_transitions(database, CONFIG, origin="https://app.test", at=at) == 0
        assert len(events(database, "pending")) == 1
        next_day = at + timedelta(days=1)
        board["rows"] = {"AAAA": stock("AAAA", "running"), "BBBB": stock("BBBB", "running")}
        assert queue_transitions(database, CONFIG, origin="https://app.test", at=next_day) == 0


def test_a_transition_older_than_the_window_is_retired_unheard(monkeypatch):
    board = board_of(stock("AAAA", "setup"))
    _patch_board(monkeypatch, board)
    with db.connection() as database:
        queue_transitions(database, CONFIG, origin="https://app.test", at=AT)
        board["rows"] = {"AAAA": stock("AAAA", "running")}
        monkeypatch.setattr(transition_alerts, "max_age_minutes", lambda: 0)
        # A window of zero is "never expire"; use a real one to age the event out.
        monkeypatch.setattr(transition_alerts, "max_age_minutes", lambda: 60)
        monkeypatch.setattr(transition_alerts, "_transition_waiting", lambda *_: True)
        queue_transitions(database, CONFIG, origin="https://app.test", at=AT + timedelta(minutes=5))
        queue_transitions(database, CONFIG, origin="https://app.test", at=AT + timedelta(hours=3))
        assert [row["status"] for row in events(database)] == ["stale"]


def test_the_transition_segment_follows_the_market_report():
    order = {"market_report": True, "transition": True, "event": True, "runner": True}
    assert next_segment(order) == "market_report"
    order.pop("market_report")
    assert next_segment(order) == "transition"
    assert next_segment(order, last_kind="transition") == "event"


def test_the_card_is_delivered_with_one_url_and_expires(monkeypatch):
    board = board_of(stock("AAAA", "setup"))
    _patch_board(monkeypatch, board)
    with db.connection() as database:
        queue_transitions(database, CONFIG, origin="https://app.test", at=AT)
        board["rows"] = {"AAAA": stock("AAAA", "running")}
        queue_transitions(database, CONFIG, origin="https://app.test", at=AT + timedelta(minutes=1))
    sent = []
    result = telegram_outbox.deliver_outbox(
        CONFIG,
        lambda _c, text: sent.append(text) or 7,
        at=AT + timedelta(minutes=2),
        kinds=("transition",),
    )
    assert result["status"] == "sent"
    assert sent[0].count("https://") == 1
    assert "app.test/t/AAAA" in sent[0]
    assert "expires_at" in result["items"][0]


def test_formatter_stock_copy_and_escaping():
    text = format_transition_post_md(
        {
            "market": "stock",
            "ticker": "BRK.B",
            "event": "running",
            "from_tag": "setup",
            "to_tag": "running",
            "met": 9,
            "total": 9,
            "relative_volume": 4.25,
        },
        origin="https://app.test/",
    )
    assert text.startswith("⚡ *$BRK\\.B: Setup → Running*")
    assert "9/9 standards met  ·  volume 4\\.2× average" in text
    assert text.endswith("(https://app.test/t/BRK.B)")
    assert text.count("https://") == 1


def test_formatter_ratification_copy():
    base = {"market": "stock", "ticker": "SOUN", "met": 8, "total": 9}
    new = format_transition_post_md({**base, "event": "newly_ratified"}, origin="https://x.test")
    lost = format_transition_post_md(
        {**base, "event": "lost_ratification"}, origin="https://x.test"
    )
    assert new.startswith("✅ *$SOUN is newly ratified*")
    assert lost.startswith("⚠️ *$SOUN lost its ratification*")


def test_formatter_leads_a_coin_with_its_address():
    text = format_transition_post_md(
        {
            "market": "memecoin",
            "subject": MINT,
            "coin_id": "chain-abc",
            "event": "setup",
            "from_tag": "watch",
            "to_tag": "setup",
            "met": 3,
            "total": 4,
            "liquidity_usd": 48_000,
            "volume_24h": 1_200_000,
            "symbol": "SCAM_[x]",
        },
        origin="https://app.test",
    )
    lines = text.split("\n\n")
    assert lines[0] == "\U0001f535 *Memecoin: Watch → Setup*"
    assert lines[1] == f"`{MINT}`"
    assert "SCAM" not in text
    assert "liquidity $48\\.0K" in text and "24h volume $1\\.2M" in text
    assert text.endswith("(https://app.test/memecoins/coin/chain-abc)")


def test_formatter_refuses_an_unsafe_coin_address():
    item = {"market": "memecoin", "subject": "a`b", "coin_id": "c", "event": "running"}
    assert format_transition_post_md(item, origin="https://x.test") == ""


def test_memecoin_transitions_need_the_memecoin_flag(monkeypatch):
    seen = []
    monkeypatch.setattr(transition_alerts, "_stock_observations", lambda *_: {})
    monkeypatch.setattr(
        transition_alerts,
        "_memecoin_observations",
        lambda *_: seen.append("read") or {},
    )
    with db.connection() as database:
        queue_transitions(database, CONFIG, origin="https://x.test", at=AT)
        assert seen == []
        monkeypatch.setenv("TELEGRAM_MEMECOIN_ALERTS", "1")
        queue_transitions(database, CONFIG, origin="https://x.test", at=AT)
    assert seen == ["read"]


def test_memecoin_observations_read_the_saved_quote():
    quote = {
        "token_address": MINT,
        "early": {"state": "setup", "score": 55},
        "ratification": {"ratified": True, "met": 4, "total": 4},
        "liquidity_usd": 10_000,
        "volume_24h": 50_000,
    }
    with db.connection() as database:
        database.execute(
            "INSERT INTO memecoin_assets(coin_id,quote_json,collected_at,run_id) VALUES(?,?,?,?)",
            ("chain-abc", json.dumps(quote), AT.isoformat(), "run"),
        )
        found = transition_alerts._memecoin_observations(database, AT)
    assert found[MINT]["coin_id"] == "chain-abc"
    assert found[MINT]["tag"] == "setup" and found[MINT]["ratified"] is True


def test_halts_are_followed_only_for_ratified_or_called_stocks():
    with db.connection() as database:
        run_detect(database, [stock("GOOD", ratified=True), stock("BADD", ratified=False)], AT)
        rows = [
            {"ticker": "GOOD", "event_type": "trading_halt"},
            {"ticker": "BADD", "event_type": "trading_halt"},
            {"ticker": "NEWS", "event_type": "news_article"},
        ]
        assert telegram_outbox._followed_halt_tickers(database, rows) == {"GOOD"}
