"""Stock standards: RATi Rules for a stock.

Five standards, all required, over facts the caller gathers: the listing,
SEC filings and financial facts, and halts. Not an endorsement or advice.

The scanner's evidence gate is not one of them: it asks whether today's
momentum setup is backed up (volume, news, a current quote), so it would fail
every stock whenever markets are closed.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ratitrust import rules
from ratitrust.summary import summarize

# Banks, lenders, insurers and real estate: lending and premiums run through
# operating cash flow, so a burn-based cash runway says nothing about them.
# Callers decide applicability with is_financial() and pass runway_applies.
_LOW, _HIGH = rules.standard("stock", "cash")["not_applied"]["value"]
FINANCIAL_SIC = range(_LOW, _HIGH + 1)


def is_foreign_issuer(forms: set[str]) -> bool:
    """Files as a foreign private issuer: 20-F, 40-F or 6-K, and no 10-Q or 10-K."""

    upper = {form.upper() for form in forms}
    return bool(upper & FOREIGN_FORMS) and not upper & PERIODIC_FORMS


def is_financial(sic: Any) -> bool:
    try:
        return int(str(sic).strip()) in FINANCIAL_SIC
    except (TypeError, ValueError):
        return False


MAJOR_EXCHANGES = set(rules.listed("stock", "major_exchanges"))
REPORT_DAYS = int(rules.value("stock", "filings", "days_since_periodic_report"))
# A foreign private issuer files one annual report (20-F or 40-F), due four
# months after its fiscal year ends, so consecutive reports can sit up to about
# sixteen months apart. Interim results arrive on 6-K, which carries no form
# type we can tell apart from other current reports, so only the annual is read.
FOREIGN_REPORT_DAYS = int(rules.value("stock", "filings", "days_since_annual_report"))
PERIODIC_FORMS = set(rules.listed("stock", "periodic_forms"))
FOREIGN_ANNUAL_FORMS = set(rules.listed("stock", "foreign_annual_forms"))
FOREIGN_FORMS = FOREIGN_ANNUAL_FORMS | set(rules.listed("stock", "foreign_current_forms"))
# A sweep of delisting notices older than this no longer vouches for the window.
DELISTING_SWEEP_FRESH_HOURS = int(rules.asset("stock")["params"]["delisting_sweep_fresh_hours"])
MIN_RUNWAY_MONTHS = float(rules.value("stock", "cash", "cash_runway"))
MAX_SHARE_GROWTH_PCT = float(rules.value("stock", "dilution", "share_growth"))
HALT_DAYS = int(rules.value("stock", "trading", "trading_halt"))
DELISTING_DAYS = int(rules.value("stock", "trading", "delisting_notice"))
LABELS = rules.labels("stock")
NOTE = rules.asset("stock")["note"]
# A stock that misses a standard must not carry a note that reads as ratified.
UNRATIFIED_NOTE = rules.asset("stock")["unratified_note"]


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
    delistings_read: bool = True,
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
        # No notice is only known once the 90-day window has been read.
        "trading": False
        if halted_on is not None or delisting_on is not None
        else True
        if delistings_read
        else None,
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
        "trading": ", ".join(trading)
        if trading or delistings_read
        else "delisting notices not read yet",
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
