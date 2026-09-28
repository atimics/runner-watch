"""Ratified: a stock that meets the RATi Rules, from data already collected.

The standards are the RATi Rules' own (the vendored `ratitrust` package, at the
version in force); this module gathers the facts they read: the listing, SEC
filings and financial facts, halts and delisting notices.

The scanner's evidence gate is not one of them: it asks whether today's
momentum setup is backed up (volume, news, a current quote), so it fails
every stock whenever markets are closed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from ratitrust.stock import (
    DELISTING_DAYS,
    FOREIGN_ANNUAL_FORMS,
    FOREIGN_FORMS,
    HALT_DAYS,
    PERIODIC_FORMS,
    _day,
    standards,
)
from runner_web.issuer_risk import issuer_risk_contexts
from runner_web.sec_delistings import notices_read

__all__ = ["standards", "stock_ratifications"]


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
    read = notices_read(database, at)
    _note_missing_sectors(database, companies, at)
    return {
        ticker: standards(
            exchange=companies[ticker]["exchange"] if ticker in companies else None,
            issuer=issuers.get(ticker) or {},
            halted_on=halts.get(ticker),
            delisting_on=delistings.get(ticker),
            today=at.date(),
            filed=periodic.get(ticker),
            delistings_read=read,
        )
        for ticker in tickers
    }
