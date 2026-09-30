"""Foreign private issuers: 6-K notices, halts and forms (#469, recovered from #470).

A standard that cannot see a foreign issuer's filings must not report it as
failing, and must not let it pass unseen either.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from runner_watch.edgar import LISTING_NOTICE_ITEM, classify_filing, is_listing_notice
from runner_web import stock_ratify

AT = datetime(2026, 9, 27, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "On September 10, 2026, the Company received a notification letter from the "
        "Listing Qualifications Department of The Nasdaq Stock Market stating that the "
        "Company is not in compliance with Nasdaq Listing Rule 5550(a)(2).",
        "<p>the Company&nbsp;received a Staff Determination letter from Nasdaq</p>",
        "The NYSE notified the Company that it did not meet the continued listing "
        "standards set forth in Section 802.01C.",
        "The Company has received notice of delisting from the exchange.",
    ],
)
def test_a_6k_listing_notice_is_recognised(text):
    assert is_listing_notice(text)


@pytest.mark.parametrize(
    "text",
    [
        "The Company announced that it has regained compliance with Nasdaq Listing "
        "Rule 5550(a)(2).",
        "The Company reports unaudited interim results for the six months ended June 30.",
        "The shares are listed on the Nasdaq Global Market under the symbol SHMD.",
    ],
)
def test_ordinary_6ks_are_not_listing_notices(text):
    assert not is_listing_notice(text)


def test_foreign_offering_and_late_filing_forms_are_flagged():
    assert classify_filing("F-1")["sentiment"] == "risk"
    assert classify_filing("F-3/A")["kind"] == "Offering or dilution filing"
    assert classify_filing("NT 20-F")["kind"] == "Late periodic report"


@pytest.fixture
def database(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE sec_companies (cik INTEGER, ticker TEXT, name TEXT, exchange TEXT, sic TEXT);
        CREATE TABLE public_market_events (
            ticker TEXT, event_type TEXT, event_at TEXT, payload_json TEXT DEFAULT '{}'
        );
        CREATE TABLE sec_filings (ticker TEXT, form TEXT, items TEXT, filed_at TEXT);
        CREATE TABLE worker_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        INSERT INTO worker_state VALUES ('sec_delisting_sweep',
            '{"checked_at": "2026-09-27T06:00:00+00:00"}', '2026-09-27T06:00:00+00:00'),
            ('sec_6k_notices_read_from', '{"from": "2026-06-01T00:00:00+00:00"}', '2026-06-01');
        INSERT INTO sec_companies VALUES (1,'SHMD','SCHMID Group N.V.','Nasdaq','3559'),
            (2,'NTCE','Notice N.V.','Nasdaq','3559'), (3,'PAWS','Paused Co','Nasdaq','3559');
        INSERT INTO sec_filings VALUES ('SHMD','20-F','','2026-04-28T20:00:00+00:00'),
            ('NTCE','20-F','','2026-04-28T20:00:00+00:00'),
            ('NTCE','6-K','listing-notice','2026-09-10T20:00:00+00:00'),
            ('SHMD','6-K','','2026-09-12T20:00:00+00:00');
        """
    )
    healthy = {
        "issuer_data_available": True,
        "cash_runway_months": 20.0,
        "shares_growth_pct": 2.0,
        "operating_cash_flow": -1.0,
        "foreign_issuer": True,
    }
    monkeypatch.setattr(
        stock_ratify, "issuer_risk_contexts", lambda _db, tickers: dict.fromkeys(tickers, healthy)
    )
    return connection


def _trading(found, ticker):
    return next(s for s in found[ticker]["standards"] if s["key"] == "trading")


def test_a_6k_listing_notice_fails_the_trading_standard(database):
    assert LISTING_NOTICE_ITEM == "listing-notice"

    found = stock_ratify.stock_ratifications(
        database, [{"ticker": "NTCE"}, {"ticker": "SHMD"}], at=AT
    )

    assert _trading(found, "NTCE")["met"] is False
    assert _trading(found, "NTCE")["detail"] == "delisting notice Sep 10"
    # An ordinary 6-K is not a notice.
    assert _trading(found, "SHMD")["met"] is True


def test_a_volatility_pause_is_not_a_trading_halt(database):
    # Live case: SHMD read "halted Sep 25" on a +29.9% day.
    database.executemany(
        "INSERT INTO public_market_events VALUES (?,?,?,?)",
        [
            (
                "SHMD",
                "trading_halt",
                "2026-09-25T15:02:00+00:00",
                json.dumps({"reason_code": "LUDP"}),
            ),
            (
                "PAWS",
                "trading_halt",
                "2026-09-24T15:02:00+00:00",
                json.dumps({"reason_code": "LUDP"}),
            ),
            (
                "PAWS",
                "trading_halt",
                "2026-09-20T14:00:00+00:00",
                json.dumps({"reason_code": "T1"}),
            ),
        ],
    )

    found = stock_ratify.stock_ratifications(
        database, [{"ticker": "SHMD"}, {"ticker": "PAWS"}], at=AT
    )

    assert _trading(found, "SHMD")["met"] is True
    # A news-pending halt still counts, and the later pause does not move its date.
    assert _trading(found, "PAWS")["met"] is False
    assert _trading(found, "PAWS")["detail"] == "halted Sep 20"


def test_a_foreign_issuer_is_not_checked_until_6k_texts_cover_the_window(database):
    # Notices are read from 6-K text only as filings arrive, so before that
    # reading covers 90 days, "no notice" is not known: never a silent pass.
    database.execute("DELETE FROM worker_state WHERE key='sec_6k_notices_read_from'")

    found = stock_ratify.stock_ratifications(database, [{"ticker": "SHMD"}], at=AT)

    assert _trading(found, "SHMD")["met"] is None
    assert _trading(found, "SHMD")["detail"] == "delisting notices not read yet"


def test_a_domestic_issuer_does_not_wait_for_6k_reading(database, monkeypatch):
    database.execute("DELETE FROM worker_state WHERE key='sec_6k_notices_read_from'")
    domestic = {
        "issuer_data_available": True,
        "cash_runway_months": 20.0,
        "shares_growth_pct": 2.0,
        "operating_cash_flow": -1.0,
        "foreign_issuer": False,
        "periodic_filed_at": "2026-08-01T00:00:00+00:00",
        "periodic_form": "10-Q",
    }
    monkeypatch.setattr(
        stock_ratify, "issuer_risk_contexts", lambda _db, tickers: dict.fromkeys(tickers, domestic)
    )
    database.execute("DELETE FROM sec_filings WHERE ticker='SHMD'")
    database.execute(
        "INSERT INTO sec_filings VALUES ('SHMD','10-Q','','2026-08-01T00:00:00+00:00')"
    )

    found = stock_ratify.stock_ratifications(database, [{"ticker": "SHMD"}], at=AT)

    assert _trading(found, "SHMD")["met"] is True


def test_a_later_reading_does_not_shorten_the_covered_window(database):
    from runner_web.sec_delistings import foreign_notices_read, mark_foreign_notices_read_from

    mark_foreign_notices_read_from(database, datetime(2026, 9, 27, tzinfo=UTC))

    assert foreign_notices_read(database, AT)


def _cash_and_filings(found, ticker):
    by_key = {item["key"]: item for item in found[ticker]["standards"]}
    return by_key["filings"], by_key["cash"]


def test_a_foreign_issuer_with_only_6ks_is_not_failed_on_filings(database, monkeypatch):
    # Interim results are 6-Ks; with no 20-F held, the filings standard is
    # "not checked yet", never "no report in its filings".
    only_6k = {
        "issuer_data_available": True,
        "cash_runway_months": 20.0,
        "shares_growth_pct": 2.0,
        "operating_cash_flow": -1.0,
        "foreign_issuer": True,
        "sic": "3559",
    }
    monkeypatch.setattr(
        stock_ratify, "issuer_risk_contexts", lambda _db, tickers: dict.fromkeys(tickers, only_6k)
    )
    database.execute("DELETE FROM sec_filings WHERE ticker='SHMD' AND form='20-F'")

    found = stock_ratify.stock_ratifications(database, [{"ticker": "SHMD"}], at=AT)

    assert _cash_and_filings(found, "SHMD")[0]["met"] is None


def test_a_foreign_issuer_with_a_recent_20f_still_meets_filings(database):
    found = stock_ratify.stock_ratifications(database, [{"ticker": "SHMD"}], at=AT)

    assert _cash_and_filings(found, "SHMD")[0]["met"] is True


def test_a_foreign_issuer_without_a_sic_code_is_not_judged_on_cash(database, monkeypatch):
    # A foreign bank's operating cash flow is not a burn. Until its SIC code is
    # read it cannot be told from an operating company: not checked yet.
    burning = {
        "issuer_data_available": True,
        "cash_runway_months": 2.0,
        "shares_growth_pct": 2.0,
        "operating_cash_flow": -9.0,
        "foreign_issuer": True,
        "periodic_filed_at": "2026-04-28T20:00:00+00:00",
        "periodic_form": "20-F",
    }
    monkeypatch.setattr(
        stock_ratify, "issuer_risk_contexts", lambda _db, tickers: dict.fromkeys(tickers, burning)
    )

    found = stock_ratify.stock_ratifications(database, [{"ticker": "SHMD"}], at=AT)
    assert _cash_and_filings(found, "SHMD")[1]["met"] is None

    with_code = {**burning, "sic": "3559"}
    monkeypatch.setattr(
        stock_ratify, "issuer_risk_contexts", lambda _db, tickers: dict.fromkeys(tickers, with_code)
    )
    found = stock_ratify.stock_ratifications(database, [{"ticker": "SHMD"}], at=AT)
    assert _cash_and_filings(found, "SHMD")[1]["met"] is False


def test_a_domestic_issuer_is_judged_exactly_as_before(database, monkeypatch):
    # No SIC and a burn: a domestic issuer's cash still reads "not met".
    domestic = {
        "issuer_data_available": True,
        "cash_runway_months": 2.0,
        "shares_growth_pct": 2.0,
        "operating_cash_flow": -9.0,
        "foreign_issuer": False,
        "periodic_filed_at": "2020-01-01T00:00:00+00:00",
        "periodic_form": "10-Q",
    }
    monkeypatch.setattr(
        stock_ratify, "issuer_risk_contexts", lambda _db, tickers: dict.fromkeys(tickers, domestic)
    )
    database.execute("DELETE FROM sec_filings WHERE ticker='SHMD'")
    database.execute(
        "INSERT INTO sec_filings VALUES ('SHMD','10-Q','','2020-01-01T00:00:00+00:00')"
    )

    filings, cash = _cash_and_filings(
        stock_ratify.stock_ratifications(database, [{"ticker": "SHMD"}], at=AT), "SHMD"
    )

    assert cash["met"] is False
    assert filings["met"] is False  # a stale 10-Q is still stale
