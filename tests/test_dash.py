from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from runner_web import dash, db
from runner_web.db import connection, init_db
from runner_web.flash_wallet import spend_flash
from runner_web.sectors import (
    companies_missing_sectors,
    refresh_company_sectors,
    sector_board,
    sector_for,
)

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)


def _fresh() -> str:
    """A price observation the Call gate will accept, which it checks in real time."""
    return datetime.now(UTC).isoformat()


@pytest.fixture(autouse=True)
def dash_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "dash.db")
    init_db()


def test_dash_gets_one_account_however_often_it_is_ensured():
    first = dash.ensure_dash_account()
    second = dash.ensure_dash_account()

    assert first["handle"] == second["handle"] == dash.DASH_HANDLE
    with connection() as database:
        users = database.execute(
            "SELECT COUNT(*) FROM users WHERE id=?", (dash.DASH_USER_ID,)
        ).fetchone()[0]
        callers = database.execute(
            "SELECT COUNT(*) FROM caller_identities WHERE user_id=?", (dash.DASH_USER_ID,)
        ).fetchone()[0]
        avatars = database.execute(
            "SELECT COUNT(*) FROM comment_avatars WHERE user_id=?", (dash.DASH_USER_ID,)
        ).fetchone()[0]
    assert (users, callers, avatars) == (1, 1, 1)


def test_dash_has_a_fixed_face_rather_than_a_rolled_one():
    first = dash.dash_avatar()
    again = dash.dash_avatar()

    assert first == again
    assert first["name"] == dash.DASH_AVATAR_NAME
    assert first["ability"] == "Pattern Mapper"


def test_dash_is_funded_once_a_day_like_a_person():
    wallet = dash.dash_wallet(at=NOW)
    assert wallet["balance"] == 100
    assert wallet["daily_claim"] == 100

    assert dash.dash_wallet(at=NOW + timedelta(hours=2))["balance"] == 100

    with connection() as database:
        spend_flash(database, dash.DASH_USER_ID, 100, kind="report", reference_id="r1")
    assert dash.dash_wallet(at=NOW + timedelta(hours=3))["balance"] == 0

    tomorrow = dash.dash_wallet(at=NOW + timedelta(days=1))
    assert tomorrow["balance"] == 100


def test_an_empty_wallet_stops_dash_spending():
    from runner_web.flash_wallet import InsufficientFlashError

    dash.dash_wallet(at=NOW)
    with connection() as database:
        spend_flash(database, dash.DASH_USER_ID, 100, kind="report", reference_id="r1")
        with pytest.raises(InsufficientFlashError):
            spend_flash(database, dash.DASH_USER_ID, 10, kind="comment", reference_id="c1")


@pytest.mark.parametrize(
    ("sic", "expected"),
    [
        (2834, "Biotech and pharma"),
        ("8731", "Biotech and pharma"),
        (7372, "Software"),
        (6770, "Blank checks and shells"),
        (1311, "Oil and gas"),
        (4813, "Transport and utilities"),
        (5812, "Retail"),
        (0, None),
        ("", None),
        (None, None),
        ("not a code", None),
    ],
)
def test_sic_codes_group_into_names_people_use(sic, expected):
    assert sector_for(sic) == expected


def _company(cik: int, ticker: str, sic: str | None = None, refreshed: str | None = None) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at,sic,sector_refreshed_at)
            VALUES(?,?,?,'NASDAQ',?,?,?)
            """,
            (cik, ticker, f"{ticker} Inc", NOW.isoformat(), sic, refreshed),
        )


class FakeEdgar:
    def __init__(self, payloads: dict[int, dict]) -> None:
        self.payloads = payloads
        self.asked: list[str] = []

    def get_json(self, url: str) -> dict:
        self.asked.append(url)
        cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
        if cik not in self.payloads:
            raise RuntimeError("not found")
        return self.payloads[cik]


def test_the_backfill_fills_in_missing_sectors():
    _company(320193, "AAA")
    _company(789019, "BBB")
    client = FakeEdgar(
        {
            320193: {"sic": "2834", "sicDescription": "Pharmaceutical Preparations"},
            789019: {"sic": "7372", "sicDescription": "Prepackaged Software"},
        }
    )

    result = refresh_company_sectors(client, at=NOW)

    assert result == {"checked": 2, "stored": 2, "missing": 0, "failed": 0}
    with connection() as database:
        rows = {
            row["ticker"]: row["sic_description"]
            for row in database.execute("SELECT ticker,sic_description FROM sec_companies")
        }
    assert rows == {"AAA": "Pharmaceutical Preparations", "BBB": "Prepackaged Software"}


def test_a_filer_without_a_sic_is_marked_checked_not_retried_forever():
    _company(1, "CCC")
    client = FakeEdgar({1: {"sicDescription": ""}})

    result = refresh_company_sectors(client, at=NOW)

    assert result["missing"] == 1
    with connection() as database:
        pending = companies_missing_sectors(database, NOW, 10)
    assert pending == []


def test_a_failed_lookup_is_left_for_next_time():
    _company(2, "DDD")
    client = FakeEdgar({})

    result = refresh_company_sectors(client, at=NOW)

    assert result == {"checked": 1, "stored": 0, "missing": 0, "failed": 1}
    with connection() as database:
        assert [row["ticker"] for row in companies_missing_sectors(database, NOW, 10)] == ["DDD"]


def test_a_fresh_sector_is_not_looked_up_again_but_a_stale_one_is():
    _company(3, "EEE", sic="2834", refreshed=NOW.isoformat())
    with connection() as database:
        assert companies_missing_sectors(database, NOW, 10) == []
        much_later = NOW + timedelta(days=400)
        assert [row["ticker"] for row in companies_missing_sectors(database, much_later, 10)] == [
            "EEE"
        ]


def test_the_board_groups_by_sector_and_keeps_unknowns_visible():
    _company(10, "BIO", sic="2834", refreshed=NOW.isoformat())
    _company(11, "GENE", sic="8731", refreshed=NOW.isoformat())
    _company(12, "SOFT", sic="7372", refreshed=NOW.isoformat())
    rows = [
        {"ticker": "BIO", "change_pct": 30.0},
        {"ticker": "GENE", "change_pct": 10.0},
        {"ticker": "SOFT", "change_pct": -4.0},
        {"ticker": "WHO", "change_pct": 2.0},
    ]

    with connection() as database:
        board = sector_board(database, rows)

    by_name = {group["sector"]: group for group in board}
    assert by_name["Biotech and pharma"]["count"] == 2
    assert by_name["Biotech and pharma"]["average_change_pct"] == 20.0
    assert by_name["Software"]["average_change_pct"] == -4.0
    assert by_name["Unclassified"]["tickers"] == ["WHO"]
    assert board[0]["sector"] == "Biotech and pharma"


def test_an_empty_board_groups_into_nothing():
    with connection() as database:
        assert sector_board(database, []) == []


def test_the_chat_tools_offer_the_market_and_sector_lookups():
    from runner_web.telegram_chat import TOOL_SCHEMA

    names = {tool["name"] for tool in TOOL_SCHEMA}
    assert {"market_now", "sector_now", "look_up_ticker", "reply", "react", "hold"} <= names


def _tradeable(ticker: str, price: float = 1.50) -> None:
    """A ticker the scanner has seen recently enough to Call."""
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at)
            VALUES(?,?,?,'NASDAQ',?)
            """,
            (abs(hash(ticker)) % 10**6, ticker, f"{ticker} Inc", NOW.isoformat()),
        )
        database.execute(
            """
            INSERT INTO ticker_quotes(
                ticker,price,observed_at,session,previous_close,change_pct,
                source,status,requested_at,collected_at
            ) VALUES(?,?,?,'REGULAR',1.0,?,'yahoo','ok',?,?)
            """,
            (ticker, price, _fresh(), 50.0, _fresh(), _fresh()),
        )


def _move_price(ticker: str, price: float) -> None:
    """Move the price in the lane the shared resolver actually reads."""
    with connection() as database:
        database.execute(
            "UPDATE ticker_quotes SET price=?,observed_at=?,collected_at=? WHERE ticker=?",
            (price, _fresh(), _fresh(), ticker),
        )


@pytest.fixture
def callable_market(monkeypatch):
    """Make _current_call_mark succeed without touching the network."""
    from runner_web import main as web_main
    from runner_web import quotes

    monkeypatch.setattr(quotes, "ticker_quote", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        web_main,
        "ticker_detail_data",
        lambda ticker: {
            "can_publish": True,
            "current": {"price": 1.50, "quote_time": _fresh()},
        },
    )


def test_dash_opens_a_call_at_the_shared_mark(callable_market):
    _tradeable("RUN")
    dash.dash_wallet(at=NOW)

    opened = dash.dash_make_call("$run", at=NOW)

    assert opened["ok"] is True
    assert opened["ticker"] == "RUN"
    assert opened["entry_price"] == 1.5
    with connection() as database:
        row = database.execute(
            "SELECT user_id,ticker,status FROM community_calls"
        ).fetchone()
    assert (row["user_id"], row["ticker"], row["status"]) == (dash.DASH_USER_ID, "RUN", "active")


def test_dash_will_not_open_two_calls_on_one_ticker(callable_market):
    _tradeable("RUN")
    dash.dash_wallet(at=NOW)
    dash.dash_make_call("RUN", at=NOW)

    again = dash.dash_make_call("RUN", at=NOW)

    assert again == {"ok": False, "reason": "already_open", "ticker": "RUN"}


def test_dash_runs_out_of_calls_for_the_day(callable_market, monkeypatch):
    monkeypatch.setattr(dash, "DAILY_CALL_LIMIT", 1)
    _tradeable("RUN")
    _tradeable("TWO")
    dash.dash_wallet(at=NOW)
    assert dash.dash_make_call("RUN", at=NOW)["ok"] is True

    second = dash.dash_make_call("TWO", at=NOW)

    assert second["ok"] is False
    assert second["reason"] == "out_of_calls_today"


def test_dash_closes_his_own_call_and_reports_the_move(callable_market):
    _tradeable("RUN")
    dash.dash_wallet(at=NOW)
    dash.dash_make_call("RUN", at=NOW)

    _move_price("RUN", 3.00)
    closed = dash.dash_close_call("RUN", at=NOW)

    assert closed["ok"] is True
    assert closed["return_pct"] == 100.0


def test_closing_nothing_says_so(callable_market):
    dash.dash_wallet(at=NOW)
    assert dash.dash_close_call("NONE", at=NOW)["reason"] == "nothing_open"


def test_a_call_needs_a_price_the_site_would_show(monkeypatch):
    from runner_web import main as web_main
    from runner_web import quotes

    monkeypatch.setattr(quotes, "ticker_quote", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        web_main,
        "ticker_detail_data",
        lambda ticker: {
            "can_publish": True,
            "current": {"price": 1.0, "quote_time": (NOW - timedelta(days=1)).isoformat()},
        },
    )
    dash.dash_wallet(at=NOW)

    refused = dash.dash_make_call("STALE", at=NOW)

    assert refused["ok"] is False
    assert refused["reason"] == "no_fresh_price"


def test_dash_comments_under_his_own_avatar_and_pays_for_it():
    dash.dash_wallet(at=NOW)

    posted = dash.dash_comment("MSGM", "hiss. volume is real, float is not.", at=NOW)

    assert posted["ok"] is True
    assert posted["spent"] == 10
    with connection() as database:
        row = database.execute(
            "SELECT user_id,ticker,source,body FROM ticker_comments"
        ).fetchone()
        balance = database.execute(
            "SELECT balance FROM flash_wallets WHERE user_id=?", (dash.DASH_USER_ID,)
        ).fetchone()[0]
    assert row["user_id"] == dash.DASH_USER_ID
    assert row["source"] == "ai_avatar"
    assert row["ticker"] == "MSGM"
    assert balance == 90


def test_dash_stops_commenting_when_the_daily_run_is_used_up(monkeypatch):
    monkeypatch.setattr(dash, "DAILY_COMMENT_LIMIT", 2)
    dash.dash_wallet(at=NOW)
    assert dash.dash_comment("AAA", "one", at=NOW)["ok"] is True
    assert dash.dash_comment("BBB", "two", at=NOW)["ok"] is True

    third = dash.dash_comment("CCC", "three", at=NOW)

    assert third["ok"] is False
    assert third["reason"] == "out_of_comments_today"


def test_an_empty_comment_is_not_posted_or_paid_for():
    dash.dash_wallet(at=NOW)
    assert dash.dash_comment("AAA", "   ", at=NOW)["reason"] == "nothing_to_say"
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM ticker_comments").fetchone()[0] == 0


def test_dash_knows_what_he_can_still_afford():
    dash.dash_wallet(at=NOW)
    dash.dash_comment("AAA", "one", at=NOW)

    budget = dash.dash_budget(at=NOW)

    assert budget["balance"] == 90
    assert budget["comments_today"] == 1
    assert budget["comments_left"] == dash.DAILY_COMMENT_LIMIT - 1
    assert budget["can_comment"] is True


def test_dash_earns_flash_from_a_winning_call_so_he_ranks(callable_market):
    """The caller board ranks on Flash earned from Calls, so this is what puts him on it."""
    _tradeable("RUN")
    dash.dash_wallet(at=NOW)
    dash.dash_make_call("RUN", at=NOW)

    _move_price("RUN", 3.00)
    dash.dash_close_call("RUN", at=NOW)

    with connection() as database:
        earned = database.execute(
            "SELECT COUNT(*) FROM flash_transactions "
            "WHERE user_id=? AND kind='runner_call_win'",
            (dash.DASH_USER_ID,),
        ).fetchone()[0]
        identity = database.execute(
            "SELECT status FROM caller_identities WHERE user_id=?", (dash.DASH_USER_ID,)
        ).fetchone()["status"]
    assert earned == 1
    assert identity == "active"
