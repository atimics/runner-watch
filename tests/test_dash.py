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
