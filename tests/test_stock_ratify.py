from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from runner_web import stock_ratify
from runner_web.stock_ratify import standards, stock_ratifications

TODAY = date(2026, 9, 27)
AT = datetime(2026, 9, 27, 12, tzinfo=UTC)
HEALTHY = {
    "issuer_data_available": True,
    "periodic_filed_at": "2026-08-12",
    "periodic_form": "10-Q",
    "cash_runway_months": 20.0,
    "operating_cash_flow": -3_000_000.0,
    "shares_growth_pct": 8.0,
}


def rated(**changes):
    facts = {
        "exchange": "Nasdaq",
        "issuer": HEALTHY,
        "halted_on": None,
        "delisting_on": None,
        "verified": True,
        "today": TODAY,
        **changes,
    }
    return standards(**facts)


def test_a_stock_meeting_all_six_standards_is_ratified():
    result = rated()

    assert result["ratified"] is True and result["met"] == result["total"] == 6
    details = {item["key"]: item["detail"] for item in result["standards"]}
    assert details["filings"] == "latest 10-Q filed Aug 12"
    assert details["cash"] == "20 months of cash"


@pytest.mark.parametrize(
    ("changes", "failing", "detail"),
    [
        ({"exchange": "OTC"}, "exchange", "OTC"),
        (
            {"issuer": {**HEALTHY, "periodic_filed_at": "2026-04-01"}},
            "filings",
            "latest 10-Q filed Apr 1",
        ),
        (
            {"issuer": {**HEALTHY, "periodic_filed_at": None}},
            "filings",
            "no quarterly or annual report in its filings",
        ),
        ({"issuer": {**HEALTHY, "cash_runway_months": 5.0}}, "cash", "5 months of cash"),
        ({"issuer": {**HEALTHY, "shares_growth_pct": 140.0}}, "dilution", "shares up 140%"),
        ({"halted_on": date(2026, 9, 20)}, "trading", "halted Sep 20"),
        ({"delisting_on": date(2026, 8, 1)}, "trading", "delisting notice Aug 1"),
        ({"verified": False}, "data", "evidence not complete"),
    ],
)
def test_one_failing_standard_withholds_it(changes, failing, detail):
    result = rated(**changes)

    assert result["ratified"] is False
    standard = next(item for item in result["standards"] if item["key"] == failing)
    assert standard["met"] is False and standard["detail"] == detail


def test_a_company_not_burning_cash_meets_the_cash_standard():
    issuer = {**HEALTHY, "cash_runway_months": None, "operating_cash_flow": 4_000_000.0}

    cash = next(s for s in rated(issuer=issuer)["standards"] if s["key"] == "cash")

    assert cash["met"] is True and cash["detail"] == "not burning cash"


def test_missing_financials_are_not_known_rather_than_met():
    result = rated(issuer={})

    unknown = {item["key"] for item in result["standards"] if item["met"] is None}
    assert result["ratified"] is False
    # Live case: JBI files 10-Qs, but we hold none of its financial facts.
    assert unknown == {"filings", "cash", "dilution"}


@pytest.fixture
def database(monkeypatch):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE sec_companies (cik INTEGER, ticker TEXT, name TEXT, exchange TEXT);
        CREATE TABLE public_market_events (ticker TEXT, event_type TEXT, event_at TEXT);
        CREATE TABLE sec_filings (ticker TEXT, form TEXT, items TEXT, filed_at TEXT);
        INSERT INTO sec_companies VALUES (1,'GOOD','Good Co','Nasdaq'),
            (2,'HALT','Halted Co','NYSE'), (3,'DLST','Delisting Co','Nasdaq');
        INSERT INTO public_market_events VALUES ('HALT','trading_halt','2026-09-20T14:00:00+00:00'),
            ('GOOD','trading_halt','2026-07-01T14:00:00+00:00');
        INSERT INTO sec_filings VALUES ('DLST','8-K','3.01,9.01','2026-08-01T20:00:00+00:00'),
            ('GOOD','8-K','1.01,9.01','2026-09-01T20:00:00+00:00');
        """
    )
    monkeypatch.setattr(
        stock_ratify,
        "issuer_risk_contexts",
        lambda _db, tickers: {ticker: HEALTHY for ticker in tickers},
    )
    return connection


def test_ratifications_come_from_the_listing_halts_and_filings(database, monkeypatch):
    from runner_web import stock_indicator

    monkeypatch.setattr(
        stock_indicator,
        "stock_indicator",
        lambda item: {"verification": {"verified": True}},
    )
    items = [{"ticker": ticker} for ticker in ("GOOD", "HALT", "DLST")]

    found = stock_ratifications(database, items, at=AT)

    assert found["GOOD"]["ratified"] is True  # its halt was in July, outside 30 days
    trading = {t: next(s for s in found[t]["standards"] if s["key"] == "trading") for t in found}
    assert trading["HALT"]["detail"] == "halted Sep 20"
    assert trading["DLST"]["detail"] == "delisting notice Aug 1"


def test_8k_items_are_read_from_the_edgar_feed():
    from runner_watch.edgar import parse_latest_filings

    feed = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
<title>8-K - Delisting Co (0000000003) (Filer)</title>
<link rel="alternate" type="text/html" href="https://www.sec.gov/Archives/edgar/data/3/000000000326000001/0000000003-26-000001-index.htm"/>
<summary type="html"> &lt;b&gt;Filed:&lt;/b&gt; 2026-09-25
&lt;b&gt;AccNo:&lt;/b&gt; 0000000003-26-000001
&lt;br&gt;Item 3.01: Notice of Delisting or Failure to Satisfy a Continued Listing Rule
&lt;br&gt;Item 9.01: Financial Statements and Exhibits
</summary>
<updated>2026-09-25T16:05:00-04:00</updated>
<category scheme="https://www.sec.gov/" label="form type" term="8-K"/>
<id>urn:tag:sec.gov,2008:accession-number=0000000003-26-000001</id>
</entry>
</feed>"""

    filing = parse_latest_filings(feed)[0]

    assert filing.items == "3.01,9.01"


def test_a_ratified_stock_row_shows_the_mark():
    from runner_web.market_screens import listing
    from tests.test_market_screens import render, sample

    stock = {**sample("stocks"), "ratification": rated()}

    assert 'class="ratified-mark"' in render(listing("stocks", [stock]))
