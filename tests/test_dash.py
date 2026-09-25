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
    # A failure is re-due tomorrow so one bad filer cannot starve the queue.
    with connection() as database:
        assert companies_missing_sectors(database, NOW, 10) == []
        tomorrow = NOW + timedelta(days=1, minutes=1)
        assert [row["ticker"] for row in companies_missing_sectors(database, tomorrow, 10)] == [
            "DDD"
        ]


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


def test_the_chat_tools_offer_the_world_and_its_expansion():
    from runner_web.telegram_chat import TOOL_SCHEMA

    names = {tool["name"] for tool in TOOL_SCHEMA}
    assert {"expand", "reply", "react", "hold"} <= names
    # The getter menu is gone: the world carries the summaries and `expand`
    # is the one read verb.
    assert {"market_now", "sector_now", "look_up_ticker"} & names == set()


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
        row = database.execute("SELECT user_id,ticker,status FROM community_calls").fetchone()
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
        row = database.execute("SELECT user_id,ticker,source,body FROM ticker_comments").fetchone()
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
            "SELECT COUNT(*) FROM flash_transactions WHERE user_id=? AND kind='runner_call_win'",
            (dash.DASH_USER_ID,),
        ).fetchone()[0]
        identity = database.execute(
            "SELECT status FROM caller_identities WHERE user_id=?", (dash.DASH_USER_ID,)
        ).fetchone()["status"]
    assert earned == 1
    assert identity == "active"


def test_recent_runners_reads_the_same_entries_the_alerts_post():
    fresh, old = datetime.now(UTC), datetime.now(UTC) - timedelta(hours=20)
    with connection() as database:
        for ticker, when in (("NEW", fresh), ("OLD", old)):
            database.execute(
                """
                INSERT INTO pulse_entries(
                    ticker,entered_at,scan_run_id,snapshot_id,price,created_at
                ) VALUES(?,?,'run','snap-'||?,1.25,?)
                """,
                (ticker, when.isoformat(), ticker, when.isoformat()),
            )

    result = dash.recent_runners()

    assert [entry["ticker"] for entry in result["entries"]] == ["NEW"]
    assert result["count"] == 1
    assert result["entries"][0]["price_on_entry"] == 1.25


def test_community_now_reports_where_people_put_their_names():
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) "
            "VALUES('u1','u1','U1','active',?)",
            (NOW.isoformat(),),
        )
        for index, (ticker, status) in enumerate(
            [("HOT", "active"), ("HOT", "closed"), ("COOL", "active")]
        ):
            # The table insists a closed Call carries its exit, which is the point.
            exit_price = 1.5 if status == "closed" else None
            exit_at = NOW.isoformat() if status == "closed" else None
            database.execute(
                """
                INSERT INTO community_calls(
                    id,public_id,user_id,ticker,side,entry_price,entry_at,
                    exit_price,exit_at,status,created_at,updated_at
                ) VALUES(?,?,'u1',?,'long',1.0,?,?,?,?,?,?)
                """,
                (
                    f"c{index}",
                    f"p{index}",
                    ticker,
                    NOW.isoformat(),
                    exit_price,
                    exit_at,
                    status,
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
        database.execute(
            "INSERT INTO comment_avatars(user_id,name,seed,ability_id,level,created_at) "
            "VALUES('u1','N','s','catalyst_scout',1,?)",
            (NOW.isoformat(),),
        )
        database.execute(
            """
            INSERT INTO ticker_comments(
                id,ticker,subject_kind,subject_key,user_id,body,status,created_at
            ) VALUES('m1','HOT','stock','HOT','u1','hi','public',?)
            """,
            (NOW.isoformat(),),
        )

    result = dash.community_now()

    hot = next(row for row in result["most_called"] if row["ticker"] == "HOT")
    assert hot["calls"] == 2
    assert hot["open_calls"] == 1
    assert hot["callers"] == 1
    assert result["most_discussed"][0] == {"ticker": "HOT", "comments": 1}


def test_session_report_says_so_when_nothing_is_frozen_yet():
    result = dash.session_report()

    assert result["found"] is False
    assert "asked_for" in result


def test_session_report_quotes_the_frozen_board(monkeypatch):
    from runner_web import dash as dash_module

    frozen = {
        "label": "Post-market recap",
        "report_day": "2026-09-11",
        "as_of_label": "4:05 PM ET",
        "headline": "MSGM led the watch board",
        "summary": "4 names carried over.",
        "analysis": {
            "headline": "One runner carried a thin group.",
            "points": ["a", "b", "c", "d"],
        },
        "forecast_record": {"label": "2-1", "win_rate": 66.7},
        "leaders": [
            {
                "ticker": "MSGM",
                "change_pct": 34.0,
                "relative_volume": 8.4,
                "eod_forecast": {"target_price": 1.85, "status": "hit"},
                "session_return_pct": 39.4,
            }
        ],
        "desk_comments": [{"name": "Keen Cobalt Mapper", "body": "held its range"}],
    }
    monkeypatch.setattr(
        dash_module,
        "session_report",
        dash_module.session_report,
    )
    import runner_web.market_reports as reports

    monkeypatch.setattr(
        reports,
        "market_reports_overview",
        lambda **_kwargs: {
            "latest": {"post_market": frozen, "pre_market": None},
            "featured": frozen,
        },
    )

    result = dash.session_report("post")

    assert result["found"] is True
    assert result["target_record"] == "2-1"
    assert result["hit_rate"] == 66.7
    assert result["board"][0]["result"] == "hit"
    assert len(result["flash_points"]) == 3
    assert result["desk_comments"][0]["who"] == "Keen Cobalt Mapper"


def test_the_chat_tools_cover_the_whole_board():
    from runner_web import dash
    from runner_web.telegram_chat import TOOL_SCHEMA

    names = {tool["name"] for tool in TOOL_SCHEMA}
    assert {
        "expand",
        "make_call",
        "close_call",
        "comment_on_ticker",
        "my_standing",
    } <= names
    # Every node the world advertises is reachable through expand.
    assert {
        "board",
        "runners",
        "events",
        "community",
        "sector:<name>",
        "report:pre",
        "report:post",
        "ticker:<SYM>",
    } <= set(dash.WORLD_NODES)


def _looked_up(ticker: str = "MSGM"):
    from runner_web.telegram_chat import look_up_ticker

    return look_up_ticker(ticker)


def test_an_unknown_ticker_is_reported_as_unknown():
    assert _looked_up("NOPE")["known"] is False
    assert _looked_up("")["known"] is False


def test_a_lookup_translates_the_scanner_jargon(monkeypatch):
    from runner_web import telegram_chat

    monkeypatch.setattr(
        telegram_chat,
        "_TRADE_STATE_PLAIN",
        telegram_chat._TRADE_STATE_PLAIN,
    )
    assert telegram_chat._TRADE_STATE_PLAIN["MANAGE"] == "already moving, handle with care"
    assert telegram_chat._RUG_PLAIN["guarded"] == "some warning signs"
    assert telegram_chat._TRADE_STATE_PLAIN["AVOID"].startswith("the scanner says")


def test_a_lookup_carries_the_whole_picture(monkeypatch):
    from runner_web import main as web_main
    from runner_web import quotes, telegram_chat

    monkeypatch.setattr(quotes, "ticker_quote", lambda *a, **k: None)
    monkeypatch.setattr(
        web_main,
        "_public_ticker_detail_data",
        lambda ticker: {
            "ticker": "MSGM",
            "company": "Motorsport Games",
            "current": {
                "price": 1.42,
                "change_pct": 34.0,
                "relative_volume": 8.4,
                "trade_state": "MANAGE",
                "rug_level": "guarded",
                "signals": ["Volume acceleration"],
                "risks": ["Thin float"],
            },
            "evidence_gate": {"summary": "Dated 8-K plus volume.", "blockers": []},
            "directional_thesis": {"label": "Up", "horizon": "close", "expected_return_pct": 4.0},
            "external_context": {"active_halt": None},
            "events": [{"form": "8-K", "filed_at": "2026-09-11", "evidence_text": "Contract win"}],
        },
    )
    monkeypatch.setattr(
        web_main,
        "daily_report_for_ticker",
        lambda ticker, viewer=None: {
            "locked": False,
            "headline": "Contract is real",
            "thesis": "Revenue is dated and confirmed.",
            "catalysts": ["Signed contract"],
            "risks": ["Dilution history"],
            "unknowns": ["Margin"],
        },
    )
    monkeypatch.setattr(
        web_main,
        "comments_for_ticker",
        lambda ticker, limit=6: [
            {
                "avatar": {"name": "Wary Obsidian Sentinel", "ability": "Risk Sentinel"},
                "body": "float is thin",
                "created_at": "2026-09-11T12:00:00+00:00",
            }
        ],
    )

    looked = telegram_chat.look_up_ticker("$msgm")

    assert looked["known"] is True
    assert looked["trade_state_means"] == "already moving, handle with care"
    assert looked["rug_means"] == "some warning signs"
    assert looked["research_report"]["headline"] == "Contract is real"
    assert looked["avatar_comments"][0]["who"] == "Wary Obsidian Sentinel"
    assert looked["filings"][0]["what"] == "Contract win"
    assert looked["model_view"]["label"] == "Up"
    assert looked["community"] == {"calls": 0, "open_calls": 0, "callers": 0, "comments": 0}


def test_a_locked_report_is_reported_but_not_read_out(monkeypatch):
    from runner_web import main as web_main
    from runner_web import telegram_chat

    monkeypatch.setattr(
        web_main,
        "daily_report_for_ticker",
        lambda ticker, viewer=None: {"locked": True, "headline": "secret", "thesis": "secret"},
    )

    report = telegram_chat._research_report("MSGM")

    assert report["exists"] is True
    assert report["readable"] is False
    assert "secret" not in str(report)


def test_a_saved_target_comes_back_with_its_result():
    from runner_web.telegram_chat import _todays_target

    with connection() as database:
        database.execute(
            """
            INSERT INTO market_session_reports(
                id,report_day,report_type,source_scan_run_id,as_of,headline,summary,
                created_at,updated_at
            ) VALUES('r','2026-09-11','pre_market','scan',?,'h','s',?,?)
            """,
            (NOW.isoformat(), NOW.isoformat(), NOW.isoformat()),
        )
        database.execute(
            """
            INSERT INTO market_report_forecasts(
                report_id,ticker,report_day,reference_price,reference_at,target_price,
                direction,reason,model,contract_version,forecast_at,status,close_price
            ) VALUES('r','MSGM','2026-09-11',1.42,?,1.85,'up','volume','m','v',?,'hit',1.98)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )

    target = _todays_target("MSGM")

    assert target["target_price"] == 1.85
    assert target["status"] == "hit"
    assert target["close_price"] == 1.98
    assert _todays_target("NONE") is None


def test_dash_carries_a_drawn_portrait_and_others_do_not():
    from runner_web.pseudonyms import AVATAR_PORTRAITS, comment_avatar_profile

    portrait = dash.dash_avatar()["portrait"]
    assert portrait == "/static/dash-cheetah.png"
    assert AVATAR_PORTRAITS[dash.DASH_AVATAR_SEED] == portrait
    assert comment_avatar_profile("Someone", "another-seed", "risk_sentinel")["portrait"] is None


def test_the_portrait_file_ships_with_the_app():
    from pathlib import Path

    image = Path(__file__).parents[1] / "web/static/dash-cheetah.png"
    assert image.exists()
    assert image.stat().st_size < 400_000


def test_the_avatar_macro_renders_a_picture_for_dash_and_a_face_for_everyone_else():
    from runner_web.main import templates
    from runner_web.pseudonyms import comment_avatar_profile

    macro = templates.env.get_template("_comment_avatar.html").module.comment_avatar

    drawn = str(macro(dash.dash_avatar()))
    generated = str(macro(comment_avatar_profile("Someone", "another-seed", "risk_sentinel")))

    assert "/static/dash-cheetah.png" in drawn
    assert "comment-avatar-portrait" in drawn
    assert "avatar-tone-" not in drawn
    assert "<img" not in generated
    assert "avatar-tone-" in generated


def test_a_hinted_name_groups_without_a_filed_sic():
    _company(20, "CURE", sic=None, refreshed=NOW.isoformat())
    with connection() as database:
        database.execute(
            "UPDATE sec_companies SET name='CureVax Therapeutics Inc' WHERE ticker='CURE'"
        )
    rows = [{"ticker": "CURE", "change_pct": 12.0}]

    with connection() as database:
        board = sector_board(database, rows)

    assert board[0]["sector"] == "Biotech and pharma"
    assert board[0]["hinted"] is True


def test_an_unknown_name_stays_unclassified():
    _company(21, "ZZZ", sic=None, refreshed=NOW.isoformat())
    rows = [{"ticker": "ZZZ", "change_pct": 1.0}]

    with connection() as database:
        board = sector_board(database, rows)

    assert board[0]["sector"] == "Unclassified"
    assert "hinted" not in board[0]


def test_recent_runners_reports_the_schedule_when_the_market_is_closed(monkeypatch):
    closed = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)  # a Saturday
    monkeypatch.setattr(dash, "_iso", lambda *_: closed.isoformat())

    payload = dash.recent_runners(at=closed)

    assert payload["count"] == 0
    assert "Weekend closed" in payload["note"]
    assert "ET" in payload["note"]
    assert "schedule" not in payload["note"] and "gap" not in payload["note"]


def test_market_now_carries_the_next_open(monkeypatch):
    closed = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)

    def fake_pulse(**_):
        return {"rows": [], "updated_at": None}

    monkeypatch.setattr("runner_web.main._public_pulse_data", fake_pulse)

    payload = dash.market_now(at=closed)

    assert payload["scanner_active"] is False
    assert "ET" in payload["next_open"]
    assert payload["eastern_now"].endswith(" ET")


def test_the_world_is_one_snapshot_of_everything():
    world = dash.dash_world(at=NOW)

    assert {
        "session",
        "board",
        "runners",
        "events",
        "community",
        "self",
        "changes",
    } <= set(world)
    assert world["session"]["label"]
    assert "any" in world["changes"]


def test_expand_reads_a_node_and_rejects_an_unknown_one():
    assert isinstance(dash.dash_expand("board"), dict)
    assert isinstance(dash.dash_expand("events"), list)
    unknown = dash.dash_expand("nope")
    assert "error" in unknown and "board" in unknown["nodes"]
    assert dash.dash_expand("ticker:NOPE").get("known") is False


def test_recent_changes_reports_what_landed():
    with connection() as database:
        database.execute(
            "INSERT INTO pulse_entries(ticker,entered_at,scan_run_id,snapshot_id,price,created_at) "
            "VALUES('AAA',?,?,?,?,?)",
            (NOW.isoformat(), "run", "snap", 1.0, NOW.isoformat()),
        )

    changes = dash.recent_changes(at=NOW)

    assert changes["new_runners"] == 1
    assert changes["any"] is True


def test_recent_actions_reads_dashs_chat_log():
    with connection() as database:
        database.execute(
            "INSERT INTO telegram_chat_actions("
            "id,chat_id,message_id,update_id,action,detail,acted_at"
            ") VALUES('a1',123,1,1,'reply','hello',?)",
            (NOW.isoformat(),),
        )

    actions = dash.recent_actions(123)

    assert actions and actions[0]["action"] == "reply"


def test_the_desk_note_speaks_only_when_something_changed(monkeypatch):
    from runner_web import main as web_main

    monkeypatch.setattr(web_main, "DASH_DESK_NOTES_ENABLED", True)
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_API_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setattr(web_main, "telegram_room_chat_id", lambda: 123)
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(
        web_main,
        "send_telegram_reply",
        lambda config, chat_id, text: sent.append((chat_id, text)),
    )
    monkeypatch.setattr(web_main, "dash_world", lambda *a, **k: {"changes": {"any": True}})
    monkeypatch.setattr(web_main, "_generate_desk_note", lambda world: "Something moved.")

    result = web_main.post_dash_desk_note(at=NOW)

    assert result["status"] == "sent"
    assert sent == [(123, "Something moved.")]

    sent.clear()
    monkeypatch.setattr(web_main, "dash_world", lambda *a, **k: {"changes": {"any": False}})
    assert web_main.post_dash_desk_note(at=NOW + timedelta(hours=2))["status"] == "quiet"
    assert sent == []


def _halt_item(ticker: str, halt: str, reason: str, resume: str = "") -> str:
    resume_tags = (
        f"<ndaq:ResumptionDate>09/11/2026</ndaq:ResumptionDate>"
        f"<ndaq:ResumptionQuoteTime>{resume}</ndaq:ResumptionQuoteTime>"
        f"<ndaq:ResumptionTradeTime>{resume}</ndaq:ResumptionTradeTime>"
        if resume
        else ""
    )
    return (
        f"<item><title>{ticker} Corp</title>"
        f"<ndaq:HaltDate>09/11/2026</ndaq:HaltDate><ndaq:HaltTime>{halt}</ndaq:HaltTime>"
        f"<ndaq:IssueSymbol>{ticker}</ndaq:IssueSymbol>"
        f"<ndaq:IssueName>{ticker} Corp Common Stock</ndaq:IssueName>"
        f"<ndaq:Market>NASDAQ</ndaq:Market><ndaq:ReasonCode>{reason}</ndaq:ReasonCode>"
        f"{resume_tags}</item>"
    )


def _seed_halts(*items: str) -> None:
    from runner_web.nasdaq_halts import refresh_trade_halts

    body = (
        '<?xml version="1.0"?><rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/">'
        f"<channel>{''.join(items)}</channel></rss>"
    ).encode()
    with connection() as database:
        database.execute(
            "UPDATE source_registry SET enabled=1 "
            "WHERE source='nasdaq_trader' AND feed='trade_halts'"
        )
    refresh_trade_halts(download=lambda _url, _timeout: (body, "application/rss+xml"))


def _seed_bar(ticker: str, minute: str, open_: float, high: float, low: float, close: float):
    with connection() as database:
        database.execute(
            "INSERT INTO market_bars(source,ticker,interval,bar_time,open,high,low,close,"
            "volume,first_collected_at,last_collected_at) "
            "VALUES('yahoo',?,'5m',?,?,?,?,?,1000,?,?)",
            (
                ticker,
                f"2026-09-11T{minute}:00+00:00",
                open_,
                high,
                low,
                close,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )


def test_dash_sees_why_a_halt_happened_when_it_reopens_and_what_followed():
    # NOW is 11:00 ET. WZRD paused twice on volatility; TDIC is waiting on news.
    _seed_halts(
        _halt_item("WZRD", "10:00:00", "LUDP", "10:05:00"),
        _halt_item("WZRD", "10:20:00", "LUDP", "10:25:00"),
        _halt_item("TDIC", "10:50:00", "T1"),
    )
    _seed_bar("WZRD", "13:55", 1.9, 2.0, 1.9, 2.0)
    _seed_bar("WZRD", "14:05", 2.4, 2.6, 2.3, 2.5)
    _seed_bar("WZRD", "14:10", 2.5, 2.5, 2.1, 2.2)
    _seed_bar("WZRD", "14:25", 2.0, 2.1, 1.8, 1.9)

    halts = {entry["ticker"]: entry for entry in dash.recent_halts(at=NOW)}

    wzrd = halts["WZRD"]
    assert wzrd["halts_today"] == 2
    assert wzrd["span_minutes"] == 20
    second, first = wzrd["halts"]
    assert first["reason"] == "volatility pause (limit up / limit down)"
    assert first["state"] == "reopened"
    assert (first["halted_at"], first["resumes_at"]) == ("10:00 ET", "10:05 ET")
    assert first["minutes_halted"] == 5
    assert first["after_reopen"]["before_halt"] == 2.0
    assert first["after_reopen"]["reopen"] == 2.4
    assert first["after_reopen"]["reopen_vs_before_pct"] == 20.0
    assert first["after_reopen"]["high"] == 2.6
    assert first["after_reopen"]["last"] == 1.9
    assert second["after_reopen"]["before_halt"] == 2.2
    assert second["after_reopen"]["reopen"] == 2.0

    tdic = halts["TDIC"]["halts"][0]
    assert tdic["reason"] == "news pending"
    assert tdic["state"] == "halted, no resume time yet"
    assert tdic["resumes_at"] is None
    assert tdic["minutes_halted"] == 10
    assert tdic["after_reopen"] is None


def test_a_scheduled_resume_is_not_read_as_a_reopen():
    _seed_halts(_halt_item("FGL", "10:55:00", "T12", "11:10:00"))

    (entry,) = dash.recent_halts(at=NOW)

    halt = entry["halts"][0]
    assert halt["reason"] == "Nasdaq asked the company for more information"
    assert halt["state"] == "resume scheduled"
    assert halt["resumes_at"] == "11:10 ET"
    assert halt["after_reopen"] is None


def test_an_unknown_halt_code_is_passed_through_rather_than_guessed():
    assert dash.halt_reason("ZZ9") == "ZZ9"
    assert dash.halt_reason("") is None


def test_recent_events_read_the_stored_payload():
    _seed_halts(_halt_item("TDIC", "10:50:00", "T1"))

    (event,) = dash.recent_events(at=NOW)

    assert event["headline"] == "TDIC Corp Common Stock"
    assert event["kind"] == "Trading halt · halted · news pending"


def test_the_world_and_expand_include_halts():
    _seed_halts(_halt_item("TDIC", "10:50:00", "T1"))

    assert dash.dash_world(at=NOW)["halts"][0]["ticker"] == "TDIC"
    assert isinstance(dash.dash_expand("halts"), list)


def _seed_game(event_id, status, start, away_score=None, home_score=None, detail="Scheduled"):
    with connection() as database:
        database.execute(
            "INSERT INTO sports_events(id,provider,external_id,league,name,start_time,status,"
            "status_detail,home_team_id,home_team_name,home_abbreviation,home_record,home_score,"
            "away_team_id,away_team_name,away_abbreviation,away_record,away_score,source_url,"
            "first_collected_at,last_collected_at) "
            "VALUES(?,'espn',?,'mlb','Cubs at Mets',?,?,?,'nym','New York Mets','NYM','80-70',?,"
            "'chc','Chicago Cubs','CHC','75-75',?,'https://espn.com',?,?)",
            (
                event_id,
                event_id,
                start.isoformat(),
                status,
                detail,
                home_score,
                away_score,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )


def _seed_prediction(event_id, observed, home_probability, selection="home", edge=0.04):
    with connection() as database:
        database.execute(
            "INSERT INTO sports_predictions(id,event_id,model_version,input_hash,selection,"
            "home_probability,away_probability,home_market_probability,"
            "away_market_probability,edge,signal,quality,observed_at) "
            "VALUES(?,?,'m1',?,?,?,?,0.55,0.45,?,'lean','ok',?)",
            (
                f"{event_id}:{observed.isoformat()}",
                event_id,
                observed.isoformat(),
                selection,
                home_probability,
                1 - home_probability,
                edge,
                observed.isoformat(),
            ),
        )


def test_dash_sees_live_games_whats_next_with_the_pregame_lean_and_finals():
    _seed_game("mlb:live", "in", NOW - timedelta(hours=1), 2, 3, "Top 6th")
    _seed_game("mlb:next", "pre", NOW + timedelta(hours=3))
    _seed_game("mlb:done", "post", NOW - timedelta(hours=8), 7, 1, "Final")
    _seed_prediction("mlb:next", NOW - timedelta(hours=2), 0.52)
    _seed_prediction("mlb:next", NOW - timedelta(minutes=5), 0.59)
    # A read taken after the start must never replace the pregame lean.
    _seed_prediction("mlb:live", NOW - timedelta(hours=2), 0.6)
    _seed_prediction("mlb:live", NOW, 0.2, selection="away")

    sports = dash.sports_now(at=NOW)

    (live,) = sports["live"]
    assert live["score"] == "CHC 2, NYM 3"
    assert live["status"] == "Top 6th"
    assert live["model"]["lean"] == "New York Mets"
    (upcoming,) = sports["up_next"]
    assert upcoming["matchup"] == "CHC at NYM"
    assert "score" not in upcoming
    assert upcoming["model"] == {
        "home_win_pct": 59.0,
        "signal": "lean",
        "lean": "New York Mets",
        "market_home_win_pct": 55.0,
        "edge_pct": 4.0,
    }
    assert sports["finals"][0]["score"] == "CHC 7, NYM 1"
    assert sports["model_record"]["settled"] == 0


def test_an_unknown_league_is_named_rather_than_empty():
    assert "error" in dash.sports_now(league="cricket", at=NOW)


def _seed_coins(*coins):
    import json

    from runner_web.memecoins import normalize_memecoins

    rows = normalize_memecoins(
        [
            {
                "id": coin_id,
                "symbol": coin_id[:4],
                "name": coin_id.title(),
                "current_price": 0.01,
                "price_change_percentage_24h": change,
                "total_volume": volume,
                "market_cap": volume * 10,
                "last_updated": NOW.isoformat(),
            }
            for coin_id, change, volume in coins
        ]
    )
    rows[0]["token_address"] = "So1anaMint"
    snapshot = {"rows": rows, "collected_at": NOW.isoformat(), "run_id": "run-1"}
    forensics = {
        "analyzed_events": 1,
        "findings": [{"kind": "bundle", "title": "Bundled launch", "token_address": "So1anaMint"}],
    }
    with connection() as database:
        for key, value in (("memecoins_snapshot", snapshot), ("memecoin_forensics", forensics)):
            database.execute(
                "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
                (key, json.dumps(value), NOW.isoformat()),
            )


def test_dash_sees_the_memecoin_board_movers_and_flags(monkeypatch):
    monkeypatch.setenv("MEMECOINS_ENABLED", "true")
    _seed_coins(("dogecoin", -4.0, 900.0), ("pepe", 31.5, 500.0), ("bonk", 2.0, 100.0))

    coins = dash.memecoins_now(at=NOW)

    assert coins["status"] == "ok"
    assert [coin["id"] for coin in coins["most_traded"]] == ["dogecoin", "pepe", "bonk"]
    assert [coin["id"] for coin in coins["top_gainers"]] == ["pepe", "bonk"]
    assert [coin["id"] for coin in coins["top_losers"]] == ["dogecoin"]
    assert coins["flagged"][0]["flags"] == ["Bundled launch"]
    assert coins["calls"] == []


def test_a_disabled_memecoin_feed_says_so(monkeypatch):
    monkeypatch.setenv("MEMECOINS_ENABLED", "false")

    coins = dash.memecoins_now(at=NOW)

    assert coins["status"] == "disabled"
    assert coins["most_traded"] == []


def test_expand_finds_a_coin_by_address_and_never_by_its_creator_set_name(monkeypatch):
    import json

    monkeypatch.setenv("MEMECOINS_ENABLED", "true")
    _seed_coins(("pepe", 31.5, 500.0), ("copycat", 2.0, 100.0))
    real = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    copy = "DezXAZuKq8vGmRQCZ3tYkLpWbNe5Hs7Dr2FjAoPcpump"
    with connection() as database:
        saved = database.execute(
            "SELECT value FROM worker_state WHERE key='memecoins_snapshot'"
        ).fetchone()
        snapshot = json.loads(saved["value"])
        for row, address in zip(snapshot["rows"], (real, copy), strict=True):
            row.update(token_address=address, claimed_symbol="BONK", claimed_name="Bonk")
        database.execute(
            "UPDATE worker_state SET value=? WHERE key='memecoins_snapshot'",
            (json.dumps(snapshot),),
        )

    found = dash.dash_expand("coin:" + real)
    assert found["id"] == "pepe"
    assert found["contract_address"] == real
    assert found["symbol"] == real[:6] + "…" + real[-6:]
    assert found["creator_set_name_unverified"] == "BONK"

    by_name = dash.dash_expand("coin:$BONK")
    assert by_name["known"] is False
    assert [row["contract_address"] for row in by_name["same_name_candidates"]] == [real, copy]
    assert "contract address" in by_name["note"]
    assert dash.dash_expand("coin:" + real.lower())["known"] is False
    assert dash.dash_expand("coin:nope")["known"] is False
    assert set(dash.dash_expand("sports")) >= {"live", "up_next", "finals"}
    assert "sports" in dash.dash_world(at=NOW) and "memecoins" in dash.dash_world(at=NOW)


def test_a_reopen_wakes_dash_even_when_the_halt_itself_is_old_news():
    # Halted at 09:00 ET, reopened at 10:30 ET. NOW is 11:00 ET, so only the
    # reopen falls in the last hour.
    _seed_halts(_halt_item("WZRD", "09:00:00", "T1", "10:30:00"))

    changes = dash.recent_changes(at=NOW)

    assert changes["events"] == 0
    assert changes["reopened_halts"] == 1
    assert changes["any"] is True


def test_a_scheduled_reopen_does_not_wake_dash_early():
    _seed_halts(_halt_item("WZRD", "09:00:00", "T1", "11:30:00"))

    changes = dash.recent_changes(at=NOW)

    assert changes["reopened_halts"] == 0
    assert changes["any"] is False
