"""Foreign private issuers: IFRS facts, reporting currencies and 6-K notices (#469).

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
from runner_web.currency import amount
from runner_web.issuer_risk import build_issuer_risk_context
from runner_web.sec_facts import parse_company_facts

AT = datetime(2026, 9, 27, 12, tzinfo=UTC)


def _entry(val, end, filed, accn, form="20-F", start=None):
    entry = {"val": val, "end": end, "filed": filed, "accn": accn, "form": form}
    return {**entry, "start": start} if start else entry


def _rows(facts):
    return [
        {
            "concept": fact.concept,
            "value": fact.value,
            "unit": fact.unit,
            "period_start": fact.period_start.isoformat() if fact.period_start else None,
            "period_end": fact.period_end.isoformat(),
            "filed_at": fact.filed_at.isoformat(),
            "form": fact.form,
        }
        for fact in facts
    ]


# An IFRS filer reporting in euros, as SCHMID Group (SHMD) does.
IFRS_EUR = {
    "cik": 1987270,
    "facts": {
        "ifrs-full": {
            "CashAndCashEquivalents": {
                "units": {"EUR": [_entry(12_000_000, "2025-12-31", "2026-04-28", "a")]}
            },
            "CashFlowsFromUsedInOperatingActivities": {
                "units": {
                    "EUR": [
                        _entry(-24_000_000, "2025-12-31", "2026-04-28", "a", start="2025-01-01")
                    ]
                }
            },
            "CurrentAssets": {
                "units": {"EUR": [_entry(60_000_000, "2025-12-31", "2026-04-28", "a")]}
            },
            "CurrentLiabilities": {
                "units": {"EUR": [_entry(40_000_000, "2025-12-31", "2026-04-28", "a")]}
            },
            # Per-share units are not amounts and stay out.
            "BasicEarningsLossPerShare": {
                "units": {"EUR/shares": [_entry(-0.5, "2025-12-31", "2026-04-28", "a")]}
            },
        },
        "dei": {
            "EntityCommonStockSharesOutstanding": {
                "units": {
                    "shares": [
                        _entry(43_000_000, "2025-12-31", "2026-04-28", "a"),
                        _entry(40_000_000, "2024-12-31", "2025-04-30", "b"),
                    ]
                }
            }
        },
    },
}


def test_ifrs_facts_are_read_in_their_reporting_currency():
    facts = parse_company_facts(IFRS_EUR, collected_at=AT)

    by_concept = {fact.concept: fact for fact in facts}
    assert by_concept["cash"].unit == "EUR"
    assert by_concept["cash"].source_tag == "ifrs-full:CashAndCashEquivalents"
    assert by_concept["operating_cash_flow"].value == -24_000_000
    assert all(fact.unit in {"EUR", "shares"} for fact in facts)

    context = build_issuer_risk_context(_rows(facts))

    # €12M of cash against €24M a year of operating outflow: six months.
    assert context["currency"] == "EUR"
    assert context["cash_runway_months"] == pytest.approx(6.0, abs=0.1)
    assert context["current_ratio"] == 1.5
    assert context["shares_growth_pct"] == 7.5
    assert context["foreign_issuer"] is True and context["periodic_form"] == "20-F"


def test_facts_in_an_older_currency_are_not_mixed_in():
    # A company that moved its presentation currency from USD to EUR.
    rows = [
        {
            "concept": "cash",
            "value": 99_000_000,
            "unit": "USD",
            "period_start": None,
            "period_end": "2024-12-31",
            "filed_at": "2025-04-30",
            "form": "20-F",
        },
        {
            "concept": "operating_cash_flow",
            "value": -12_000_000,
            "unit": "EUR",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "filed_at": "2026-04-28",
            "form": "20-F",
        },
    ]

    context = build_issuer_risk_context(rows)

    assert context["currency"] == "EUR"
    # The only cash on file is in dollars, so no euro runway is invented from it.
    assert context["cash"] is None and context["cash_runway_months"] is None


@pytest.mark.parametrize(
    ("value", "currency", "compact", "text"),
    [
        (3_000_000, "USD", False, "$3,000,000.00"),
        (12_000_000, "EUR", True, "€12.0M"),
        (-4_500, "GBP", True, "-£4.5K"),
        (2_000_000, "CAD", True, "CAD 2.0M"),
        (5, None, False, "$5.00"),
        (None, "EUR", False, "Awaiting data"),
    ],
)
def test_amounts_show_their_currency(value, currency, compact, text):
    assert amount(value, currency, compact=compact) == text


def test_the_company_panel_shows_cash_in_its_currency():
    from runner_web.main import templates

    issuer = build_issuer_risk_context(_rows(parse_company_facts(IFRS_EUR, collected_at=AT)))
    html = templates.env.from_string("{{ issuer['cash']|amount(issuer.get('currency')) }}").render(
        issuer=issuer
    )

    assert html == "€12,000,000.00"


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
