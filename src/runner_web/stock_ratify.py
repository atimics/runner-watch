"""Ratified: a stock that meets RATi's basic standards.

Five standards, all required, from data already collected: the listing, SEC
filings and financial facts, and halts. Not an endorsement or advice.

The scanner's evidence gate is not one of them: it asks whether today's
momentum setup is backed up (volume, news, a current quote), so it fails
every stock whenever markets are closed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from runner_web.issuer_risk import issuer_risk_contexts

MAJOR_EXCHANGES = {"NASDAQ", "NYSE", "NYSE AMERICAN", "NYSE MKT", "AMEX"}
REPORT_DAYS = 135
MIN_RUNWAY_MONTHS = 12.0
MAX_SHARE_GROWTH_PCT = 25.0
HALT_DAYS = 30
DELISTING_DAYS = 90
# Banks, lenders, insurers and real estate (SIC 6000-6799): lending runs through
# operating cash flow, so a burn-based runway says nothing about them.
FINANCIAL_SIC = range(6000, 6800)
LABELS = {
    "exchange": "Listed on NASDAQ, NYSE or NYSE American",
    "filings": "Up to date with SEC filings",
    "cash": "12+ months of cash, or not burning cash",
    "dilution": "Share count up 25% or less in a year",
    "trading": "No halt in 30 days and no delisting notice in 90",
}
NOTE = (
    "Ratified: meets RATi's five basic standards for a stock. "
    "Not an endorsement, a guarantee or advice."
)


def _day(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _label(day: date) -> str:
    return f"{day:%b} {day.day}"


def standards(
    *,
    exchange: str | None,
    issuer: dict[str, Any],
    halted_on: date | None,
    delisting_on: date | None,
    today: date,
    sic: str | None = None,
) -> dict[str, Any]:
    """Each standard as met, not met or not known, and whether all are met."""

    listed = (exchange or "").strip().upper()
    report_day = _day(issuer.get("periodic_filed_at"))
    runway = issuer.get("cash_runway_months")
    operating = issuer.get("operating_cash_flow")
    growth = issuer.get("shares_growth_pct")
    not_burning = operating is not None and operating >= 0
    try:
        financial = int(str(sic).strip()) in FINANCIAL_SIC
    except (TypeError, ValueError):
        financial = False
    results = {
        "exchange": None if not listed else listed in MAJOR_EXCHANGES,
        # No financial facts at all means we have not read its filings, not that
        # it has none: unknown rather than not met.
        "filings": None
        if not issuer.get("issuer_data_available")
        else report_day is not None and (today - report_day).days <= REPORT_DAYS,
        "cash": True
        if financial or not_burning
        else None
        if runway is None
        else runway >= MIN_RUNWAY_MONTHS,
        "dilution": None if growth is None else growth <= MAX_SHARE_GROWTH_PCT,
        "trading": halted_on is None and delisting_on is None,
    }
    trading = [
        *([f"halted {_label(halted_on)}"] if halted_on else []),
        *([f"delisting notice {_label(delisting_on)}"] if delisting_on else []),
    ]
    details = {
        "exchange": exchange or "",
        "filings": (
            f"latest {issuer.get('periodic_form')} filed {_label(report_day)}"
            if report_day
            else "no quarterly or annual report in its filings"
            if issuer.get("issuer_data_available")
            else ""
        ),
        "cash": "not applied: financial company"
        if financial
        else "not burning cash"
        if not_burning
        else f"{runway:.0f} months of cash"
        if runway is not None
        else "",
        "dilution": f"shares up {growth:.0f}%" if growth is not None else "",
        "trading": ", ".join(trading),
    }
    return {
        "ratified": all(result is True for result in results.values()),
        "met": sum(result is True for result in results.values()),
        "total": len(results),
        "standards": [
            {"key": key, "label": LABELS[key], "met": results[key], "detail": details[key]}
            for key in LABELS
        ],
        "note": NOTE,
        "as_of": today.isoformat(),
    }


def stock_ratifications(
    database: Any, items: list[dict[str, Any]], *, at: datetime
) -> dict[str, dict[str, Any]]:
    """Ratification for each item's ticker, from a few batched queries."""

    tickers = sorted({str(item.get("ticker") or "").upper() for item in items} - {""})
    if not tickers:
        return {}
    marks = ",".join("?" for _ in tickers)
    companies = {
        str(row["ticker"]).upper(): row
        for row in database.execute(
            f"SELECT ticker,exchange,sic FROM sec_companies WHERE UPPER(ticker) IN ({marks})",
            tickers,
        ).fetchall()
    }
    halts: dict[str, date] = {}
    for row in database.execute(
        "SELECT UPPER(ticker) AS ticker,MAX(event_at) AS latest FROM public_market_events "
        f"WHERE event_type='trading_halt' AND event_at>=? AND UPPER(ticker) IN ({marks}) "
        "GROUP BY UPPER(ticker)",
        ((at - timedelta(days=HALT_DAYS)).isoformat(), *tickers),
    ).fetchall():
        if (day := _day(row["latest"])) is not None:
            halts[row["ticker"]] = day
    delistings: dict[str, date] = {}
    for row in database.execute(
        # Patterns are bound: a bare % in SQL text is a placeholder to PostgreSQL.
        "SELECT UPPER(ticker) AS ticker,MAX(filed_at) AS latest FROM sec_filings "
        "WHERE form LIKE ? AND items LIKE ? AND filed_at>=? "
        f"AND UPPER(ticker) IN ({marks}) GROUP BY UPPER(ticker)",
        ("8-K%", "%3.01%", (at - timedelta(days=DELISTING_DAYS)).isoformat(), *tickers),
    ).fetchall():
        if (day := _day(row["latest"])) is not None:
            delistings[row["ticker"]] = day
    issuers = issuer_risk_contexts(database, tickers)
    return {
        ticker: standards(
            exchange=companies[ticker]["exchange"] if ticker in companies else None,
            issuer=issuers.get(ticker) or {},
            halted_on=halts.get(ticker),
            delisting_on=delistings.get(ticker),
            today=at.date(),
            sic=companies[ticker]["sic"] if ticker in companies else None,
        )
        for ticker in tickers
    }
