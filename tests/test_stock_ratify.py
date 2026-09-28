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
        "today": TODAY,
        **changes,
    }
    return standards(**facts)


def test_a_stock_meeting_all_five_standards_is_ratified():
    result = rated()

    assert result["ratified"] is True and result["met"] == result["total"] == 5
    # The scanner's evidence gate fails every stock while markets are closed,
    # so it is not a standard.
    assert "data" not in {item["key"] for item in result["standards"]}
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
    ],
)
def test_one_failing_standard_withholds_it(changes, failing, detail):
    result = rated(**changes)

    assert result["ratified"] is False
    standard = next(item for item in result["standards"] if item["key"] == failing)
    assert standard["met"] is False and standard["detail"] == detail


def test_a_foreign_issuer_is_held_to_its_annual_20f():
    # Live case: SHMD (SCHMID Group N.V.) files 20-F and 6-K, never 10-Q or 10-K,
    # and read "no quarterly or annual report in its filings".
    foreign = {
        **HEALTHY,
        "periodic_filed_at": "2026-04-28",
        "periodic_form": "20-F",
        "foreign_issuer": True,
    }

    result = rated(issuer=foreign)

    filings = next(s for s in result["standards"] if s["key"] == "filings")
    assert filings["met"] is True
    assert filings["detail"] == "latest 20-F filed Apr 28 · foreign issuer, annual report"
    # The same filing age fails a domestic company, whose window is 135 days.
    domestic = rated(issuer={**HEALTHY, "periodic_filed_at": "2026-04-28"})
    assert next(s for s in domestic["standards"] if s["key"] == "filings")["met"] is False


def test_a_foreign_issuer_found_only_in_the_filing_index_is_read():
    # IFRS filers may carry no us-gaap facts: the filing index still shows the 20-F.
    issuer = {**HEALTHY, "periodic_filed_at": None, "periodic_form": None}
    filed = {"filed_at": "2026-03-30", "form": "20-F", "forms": {"20-F", "6-K"}}

    filings = next(
        s for s in rated(issuer=issuer, filed=filed)["standards"] if s["key"] == "filings"
    )

    assert filings["met"] is True and filings["detail"].startswith("latest 20-F filed Mar 30")


def test_a_stale_20f_does_not_meet_the_standard():
    filed = {"filed_at": "2025-04-30", "form": "20-F", "forms": {"20-F", "6-K"}}
    issuer = {**HEALTHY, "periodic_filed_at": None, "periodic_form": None}

    filings = next(
        s for s in rated(issuer=issuer, filed=filed)["standards"] if s["key"] == "filings"
    )

    assert filings["met"] is False


def test_a_foreign_issuer_with_only_current_reports_names_the_missing_annual():
    issuer = {**HEALTHY, "periodic_filed_at": None, "periodic_form": None}
    filed = {"forms": {"6-K"}}

    filings = next(
        s for s in rated(issuer=issuer, filed=filed)["standards"] if s["key"] == "filings"
    )

    assert filings["met"] is False
    assert filings["detail"] == "foreign issuer · no 20-F or 40-F annual report in its filings"


def test_issuer_facts_from_a_20f_mark_a_foreign_issuer():
    from runner_web.issuer_risk import build_issuer_risk_context

    rows = [
        {
            "concept": "shares_outstanding",
            "value": 43_000_000,
            "period_start": None,
            "period_end": "2025-12-31",
            "filed_at": "2026-04-28",
            "form": "20-F",
        }
    ]

    context = build_issuer_risk_context(rows)

    assert context["foreign_issuer"] is True
    assert (context["periodic_form"], context["periodic_filed_at"]) == ("20-F", "2026-04-28")


def test_only_a_ratified_stock_carries_the_ratified_note():
    assert rated()["note"].startswith("Ratified:")
    failing = rated(halted_on=date(2026, 9, 25))
    assert failing["note"].startswith("Not ratified:")
    assert "meets RATi" not in failing["note"]
    # Unchecked is not met: it blocks and it does not count.
    unknown = rated(issuer={})
    assert unknown["ratified"] is False and unknown["note"].startswith("Not ratified:")
    assert unknown["met"] == sum(s["met"] is True for s in unknown["standards"])


def test_a_failing_card_never_shows_the_ratified_note():
    from runner_web.market_screens import detail
    from tests.test_market_screens import render

    stock = {"ticker": "SHMD", "price": 4.6, "ratification": rated(halted_on=TODAY)}

    page = render(detail("stocks", {"current": stock, "ticker": "SHMD"}))

    assert "Ratified: meets" not in page
    assert "Not ratified:" in page
    assert '<li class="standard-unmet">' in page


def test_a_foreign_issuer_20f_is_read_from_sec_filings(database, monkeypatch):
    database.executescript(
        """
        INSERT INTO sec_companies VALUES (5,'SHMD','SCHMID Group N.V.','Nasdaq','3559');
        INSERT INTO sec_filings VALUES ('SHMD','20-F','','2026-04-28T20:00:00+00:00'),
            ('SHMD','6-K','','2026-09-10T20:00:00+00:00');
        """
    )
    # Facts read, but none of them from a periodic report: the IFRS case.
    no_report = {**HEALTHY, "periodic_filed_at": None, "periodic_form": None}
    monkeypatch.setattr(
        stock_ratify, "issuer_risk_contexts", lambda _db, tickers: dict.fromkeys(tickers, no_report)
    )

    found = stock_ratifications(database, [{"ticker": "SHMD"}], at=AT)

    filings = next(s for s in found["SHMD"]["standards"] if s["key"] == "filings")
    assert filings["met"] is True
    assert filings["detail"] == "latest 20-F filed Apr 28 · foreign issuer, annual report"


def test_a_company_with_both_a_10q_and_a_10k_is_read_without_failing(database):
    database.executescript(
        """
        INSERT INTO sec_filings VALUES ('GOOD','10-K','','2026-03-01T20:00:00+00:00'),
            ('GOOD','10-Q','','2026-08-10T20:00:00+00:00');
        """
    )

    found = stock_ratifications(database, [{"ticker": "GOOD"}, {"ticker": "DLST"}], at=AT)

    filings = next(s for s in found["GOOD"]["standards"] if s["key"] == "filings")
    assert filings["met"] is True and "DLST" in found


def test_a_company_not_burning_cash_meets_the_cash_standard():
    issuer = {**HEALTHY, "cash_runway_months": None, "operating_cash_flow": 4_000_000.0}

    cash = next(s for s in rated(issuer=issuer)["standards"] if s["key"] == "cash")

    assert cash["met"] is True and cash["detail"] == "not burning cash"


def test_a_lender_is_not_judged_on_cash_runway():
    # Live case: RWT, a mortgage lender, read "0 months of cash" because its
    # lending runs through operating cash flow.
    from runner_web.issuer_risk import build_issuer_risk_context

    shared = build_issuer_risk_context([], sic="6798")
    issuer = {
        **HEALTHY,
        "operating_cash_flow": -900_000_000.0,
        **{key: shared[key] for key in ("cash_runway_months", "runway_applies", "financial")},
    }

    result = rated(issuer=issuer)

    cash = next(s for s in result["standards"] if s["key"] == "cash")
    # Not a pass and not a failure: shown, not counted, and not blocking.
    assert cash["applies"] is False and cash["met"] is None
    assert cash["detail"] == "financial company"
    assert result["ratified"] is True
    assert (result["met"], result["total"], result["not_applied"]) == (4, 4, 1)


def test_a_not_applied_standard_reads_as_such_on_the_page():
    from runner_web.market_screens import detail
    from tests.test_market_screens import render

    issuer = {**HEALTHY, "cash_runway_months": None, "runway_applies": False}
    stock = {"ticker": "RWT", "price": 12.0, "ratification": rated(issuer=issuer)}

    page = render(detail("stocks", {"current": stock, "ticker": "RWT"}))

    assert "Ratified · 4 of 4 met · 1 not applied" in page
    assert '<li class="standard-na"><span aria-hidden="true">–</span>' in page
    assert "12+ months of cash, or not burning cash · not applied: financial company" in page


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
        CREATE TABLE sec_companies (cik INTEGER, ticker TEXT, name TEXT, exchange TEXT, sic TEXT);
        CREATE TABLE public_market_events (ticker TEXT, event_type TEXT, event_at TEXT);
        CREATE TABLE sec_filings (ticker TEXT, form TEXT, items TEXT, filed_at TEXT);
        CREATE TABLE worker_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        INSERT INTO sec_companies VALUES (1,'GOOD','Good Co','Nasdaq','3585'),
            (2,'HALT','Halted Co','NYSE','2834'), (3,'DLST','Delisting Co','Nasdaq',NULL);
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


def test_ratifications_come_from_the_listing_halts_and_filings(database):
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


def _priority(database):
    import json

    row = database.execute(
        "SELECT value FROM worker_state WHERE key='sector_priority_tickers'"
    ).fetchone()
    return json.loads(row["value"]) if row else []


def test_stocks_seen_without_a_sic_code_are_queued_for_lookup(database):
    # DLST has no SIC code; GOOD and HALT have one.
    stock_ratifications(database, [{"ticker": "GOOD"}, {"ticker": "DLST"}], at=AT)
    assert _priority(database) == ["DLST"]

    # A single stock page adds to the list rather than replacing it...
    database.execute("INSERT INTO sec_companies VALUES (4,'RWT','Redwood Trust','NYSE',NULL)")
    stock_ratifications(database, [{"ticker": "RWT"}], at=AT)
    assert _priority(database) == ["DLST", "RWT"]

    # ...and a stock drops off once its code is in.
    database.execute("UPDATE sec_companies SET sic='6798' WHERE ticker='RWT'")
    stock_ratifications(database, [{"ticker": "RWT"}], at=AT)
    assert _priority(database) == ["DLST"]


@pytest.fixture
def sector_db(tmp_path, monkeypatch):
    from runner_web import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "sectors.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    with db.connection() as database:
        for cik, ticker in ((1, "AAA"), (2, "BBB"), (930236, "RWT")):
            database.execute(
                "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) "
                "VALUES(?,?,?,?,?)",
                (cik, ticker, ticker, "NYSE", AT.isoformat()),
            )
        # RWT was tried and came back without a code: normally not again for 90 days.
        database.execute(
            "UPDATE sec_companies SET sector_refreshed_at=? WHERE ticker='RWT'",
            (datetime(2026, 9, 1, tzinfo=UTC).isoformat(),),
        )
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
            ("sector_priority_tickers", '["RWT"]', AT.isoformat()),
        )
    return db


class Sec:
    def __init__(self, fail=()):
        self.asked, self.fail = [], set(fail)

    def get_json(self, url):
        cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
        self.asked.append(cik)
        if cik in self.fail:
            raise OSError("SEC unavailable")
        return {"sic": "6798", "sicDescription": "Real Estate Investment Trusts"}


def test_a_board_stock_without_a_code_is_looked_up_first(sector_db):
    from runner_web.sectors import refresh_company_sectors

    sec = Sec()
    refresh_company_sectors(sec, at=AT, limit=2)

    assert sec.asked[0] == 930236
    with sector_db.connection() as database:
        row = database.execute("SELECT sic FROM sec_companies WHERE ticker='RWT'").fetchone()
    assert row["sic"] == "6798"


def test_a_failed_priority_lookup_is_retried_after_six_hours(sector_db):
    from datetime import timedelta

    from runner_web.sectors import refresh_company_sectors

    refresh_company_sectors(Sec(fail={930236}), at=AT, limit=1)
    soon, later = Sec(), Sec()
    refresh_company_sectors(soon, at=AT + timedelta(hours=1), limit=1)
    refresh_company_sectors(later, at=AT + timedelta(hours=7), limit=1)

    assert 930236 not in soon.asked
    assert later.asked[0] == 930236


def test_an_unchecked_standard_reads_as_its_own_state_and_the_call_is_labelled():
    from runner_web.market_screens import detail
    from tests.test_market_screens import render

    issuer = {**HEALTHY, "cash_runway_months": None, "operating_cash_flow": None}
    stock = {"ticker": "SHMD", "price": 4.6, "ratification": rated(issuer=issuer)}

    page = render(detail("stocks", {"current": stock, "ticker": "SHMD"}))

    assert "Standards · 4 of 5 met · 1 not checked yet" in page
    assert '<li class="standard-unknown"><span aria-hidden="true">?</span>' in page
    assert "<em>not checked yet</em>, so not met" in page
    assert 'class="standards-scope">Facts, not a Call' in page
    assert '<span class="state-chip-source">Call</span>' in page
