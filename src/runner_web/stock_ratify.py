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

from runner_web.issuer_risk import (
    FOREIGN_ANNUAL_FORMS,
    FOREIGN_FORMS,
    PERIODIC_FORMS,
    is_foreign_issuer,
    issuer_risk_contexts,
)
from runner_web.ratification import summarize

MAJOR_EXCHANGES = {"NASDAQ", "NYSE", "NYSE AMERICAN", "NYSE MKT", "AMEX"}
REPORT_DAYS = 135
# A foreign private issuer files one annual report (20-F or 40-F), due four
# months after its fiscal year ends, so consecutive reports can sit up to about
# sixteen months apart. Interim results arrive on 6-K, which carries no form
# type we can tell apart from other current reports, so only the annual is read.
FOREIGN_REPORT_DAYS = 490
MIN_RUNWAY_MONTHS = 12.0
MAX_SHARE_GROWTH_PCT = 25.0
HALT_DAYS = 30
DELISTING_DAYS = 90
LABELS = {
    "exchange": "Listed on NASDAQ, NYSE or NYSE American",
    "filings": "Up to date with SEC filings",
    "cash": "12+ months of cash, or not burning cash",
    "dilution": "Share count up 25% or less in a year",
    "trading": "No halt in 30 days and no delisting notice in 90",
}
NOTE = (
    "Ratified: meets RATi's five basic standards for a stock. "
    "Standards are checks on published facts, separate from any Call. "
    "Not an endorsement, a guarantee or advice."
)
# A stock that misses a standard must not carry a note that reads as ratified.
UNRATIFIED_NOTE = (
    "Not ratified: this stock does not meet all of RATi's five basic standards, "
    "or one is not checked yet. Standards are checks on published facts, separate "
    "from any Call. Not an endorsement, a guarantee or advice."
)


def _day(value: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def _label(day: date) -> str:
    return f"{day:%b} {day.day}"


def _periodic(issuer: dict[str, Any], filed: dict[str, Any] | None) -> tuple[date | None, str]:
    """The latest periodic report from the financial facts or the filing index.

    Financial facts come from XBRL tags a foreign issuer reporting under IFRS
    may not use, so the filing index is read too: whichever is later counts.
    """

    candidates = [
        (_day(issuer.get("periodic_filed_at")), str(issuer.get("periodic_form") or "")),
        *(((_day(filed.get("filed_at")), str(filed.get("form") or "")),) if filed else ()),
    ]
    known = [(day, form.upper()) for day, form in candidates if day is not None and form]
    return max(known) if known else (None, "")


def standards(
    *,
    exchange: str | None,
    issuer: dict[str, Any],
    halted_on: date | None,
    delisting_on: date | None,
    today: date,
    filed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Each standard as met, not met or not known, and whether all are met.

    `filed` is the latest periodic filing in the filing index, with the forms
    the company files (`forms`), which tell a foreign private issuer apart.
    """

    listed = (exchange or "").strip().upper()
    report_day, report_form = _periodic(issuer, filed)
    foreign = bool(issuer.get("foreign_issuer")) or is_foreign_issuer(
        set((filed or {}).get("forms") or ())
    )
    # Only an annual report counts for a foreign issuer; a 10-Q never arrives.
    window = FOREIGN_REPORT_DAYS if foreign else REPORT_DAYS
    filings_known = bool(issuer.get("issuer_data_available")) or report_day is not None
    runway = issuer.get("cash_runway_months")
    operating = issuer.get("operating_cash_flow")
    growth = issuer.get("shares_growth_pct")
    not_burning = operating is not None and operating >= 0
    # Whether runway applies is decided with the issuer facts, for every reader.
    financial = issuer.get("runway_applies") is False
    results = {
        "exchange": None if not listed else listed in MAJOR_EXCHANGES,
        # No financial facts at all means we have not read its filings, not that
        # it has none: unknown rather than not met.
        "filings": None
        if not filings_known
        else report_day is not None and (today - report_day).days <= window,
        "cash": True if not_burning else None if runway is None else runway >= MIN_RUNWAY_MONTHS,
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
            f"latest {report_form} filed {_label(report_day)}"
            + (" · foreign issuer, annual report" if foreign else "")
            if report_day
            else "foreign issuer · no 20-F or 40-F annual report in its filings"
            if foreign and filings_known
            else "no quarterly or annual report in its filings"
            if filings_known
            else ""
        ),
        "cash": "financial company"
        if financial
        else "not burning cash"
        if not_burning
        else f"{runway:.0f} months of cash"
        if runway is not None
        else "",
        "dilution": f"shares up {growth:.0f}%" if growth is not None else "",
        "trading": ", ".join(trading),
    }
    # A lender's lending runs through operating cash flow: cash is not judged.
    return summarize(
        LABELS,
        results,
        details,
        not_applied={"cash"} if financial else set(),
        note=NOTE,
        unratified_note=UNRATIFIED_NOTE,
        as_of=today.isoformat(),
    )


def _note_missing_sectors(database: Any, companies: dict[str, Any], at: datetime) -> None:
    """Ask the sector lookup to fill these first: readers see them without a code.

    Merged into the saved list, so one stock page does not replace the board's.
    """

    import json

    missing = {ticker for ticker, row in companies.items() if not row["sic"]}
    found = set(companies) - missing
    saved = database.execute(
        "SELECT value FROM worker_state WHERE key='sector_priority_tickers'"
    ).fetchone()
    try:
        earlier = set(json.loads(saved["value"])) if saved else set()
    except (TypeError, ValueError):
        earlier = set()
    wanted = sorted((earlier | missing) - found)[:200]
    if saved and sorted(earlier) == wanted:
        return
    database.execute(
        "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        ("sector_priority_tickers", json.dumps(wanted), at.isoformat()),
    )


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
    periodic: dict[str, dict[str, Any]] = {}
    report_forms = sorted(PERIODIC_FORMS | FOREIGN_FORMS)
    form_marks = ",".join("?" for _ in report_forms)
    for row in database.execute(
        "SELECT UPPER(ticker) AS ticker,UPPER(form) AS form,MAX(filed_at) AS latest "
        f"FROM sec_filings WHERE UPPER(form) IN ({form_marks}) "
        f"AND UPPER(ticker) IN ({marks}) GROUP BY UPPER(ticker),UPPER(form)",
        (*report_forms, *tickers),
    ).fetchall():
        entry = periodic.setdefault(row["ticker"], {"forms": set()})
        entry["forms"].add(row["form"])
        # 6-K is a current report: it marks a foreign issuer but is not the report.
        if row["form"] in PERIODIC_FORMS | FOREIGN_ANNUAL_FORMS and (day := _day(row["latest"])):
            if entry.get("filed_at") is None or day.isoformat() > entry["filed_at"]:
                entry.update(filed_at=day.isoformat(), form=row["form"])
    issuers = issuer_risk_contexts(database, tickers)
    _note_missing_sectors(database, companies, at)
    return {
        ticker: standards(
            exchange=companies[ticker]["exchange"] if ticker in companies else None,
            issuer=issuers.get(ticker) or {},
            halted_on=halts.get(ticker),
            delisting_on=delistings.get(ticker),
            today=at.date(),
            filed=periodic.get(ticker),
        )
        for ticker in tickers
    }
