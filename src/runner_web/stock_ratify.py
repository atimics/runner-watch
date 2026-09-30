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
    is_foreign_issuer,
    standards,
)
from runner_watch.edgar import LISTING_NOTICE_ITEM
from runner_web.issuer_risk import issuer_risk_contexts
from runner_web.sec_delistings import foreign_notices_read, notices_read

__all__ = ["standards", "stock_ratifications"]


# Limit up-limit down and market-wide circuit breaker pauses stop trading for
# minutes because the price moved, not because of the company: they are not the
# halts this standard is about (news pending, regulatory concern, suspension).
VOLATILITY_PAUSE_CODES = {"LUDP", "LUDS", "M", "MWC0", "MWC1", "MWC2", "MWC3", "MWCQ"}


def is_volatility_pause(payload: Any) -> bool:
    import json

    try:
        values = json.loads(payload) if isinstance(payload, str) else payload or {}
    except ValueError:
        return False
    code = str(values.get("reason_code") or "").strip().upper() if isinstance(values, dict) else ""
    return code in VOLATILITY_PAUSE_CODES


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


def _foreign_inputs(
    issuer: dict[str, Any], filed: dict[str, Any] | None
) -> tuple[bool, dict[str, Any]]:
    """Whether the issuer is foreign, and its facts held back where they cannot judge it.

    Two checks would say "not met" for a foreign issuer only because RATi holds
    less of its record than of a domestic one; each is left as "not checked yet".

    - Interim results are furnished on 6-K, which cannot be told from other 6-Ks
      by form type. With no annual report (20-F or 40-F) in what RATi holds, the
      only evidence left would be an interim 6-K, so the filings standard is
      not judged.
    - The financial-company carve-out reads a SIC code. Until RATi has read the
      code, a foreign bank or insurer cannot be told from an operating company,
      and its operating cash flow means something else, so the cash standard is
      not judged.
    """

    foreign = bool(issuer.get("foreign_issuer")) or is_foreign_issuer(
        set((filed or {}).get("forms") or ())
    )
    if not foreign:
        return False, issuer
    held = dict(issuer)
    if not issuer.get("periodic_filed_at") and not (filed or {}).get("filed_at"):
        held["issuer_data_available"] = False
    if not issuer.get("sic"):
        held["cash_runway_months"] = None
        held["operating_cash_flow"] = None
    return True, held


def stock_ratifications(
    database: Any,
    items: list[dict[str, Any]],
    *,
    at: datetime,
    facts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Ratification for each item's ticker, from a few batched queries.

    When `facts` is given, it receives each ticker's inputs to the rules, for
    the record of results.
    """

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
        "SELECT UPPER(ticker) AS ticker,event_at,payload_json FROM public_market_events "
        f"WHERE event_type='trading_halt' AND event_at>=? AND UPPER(ticker) IN ({marks})",
        ((at - timedelta(days=HALT_DAYS)).isoformat(), *tickers),
    ).fetchall():
        if is_volatility_pause(row["payload_json"]):
            continue
        if (day := _day(row["event_at"])) is not None and day > halts.get(row["ticker"], date.min):
            halts[row["ticker"]] = day
    delistings: dict[str, date] = {}
    for row in database.execute(
        # Patterns are bound: a bare % in SQL text is a placeholder to PostgreSQL.
        # A domestic issuer's notice is 8-K item 3.01; a foreign issuer's is a
        # 6-K whose text reads as one (marked at ingestion).
        "SELECT UPPER(ticker) AS ticker,MAX(filed_at) AS latest FROM sec_filings "
        "WHERE ((form LIKE ? AND items LIKE ?) OR (form LIKE ? AND items LIKE ?)) "
        f"AND filed_at>=? AND UPPER(ticker) IN ({marks}) GROUP BY UPPER(ticker)",
        (
            "8-K%",
            "%3.01%",
            "6-K%",
            f"%{LISTING_NOTICE_ITEM}%",
            (at - timedelta(days=DELISTING_DAYS)).isoformat(),
            *tickers,
        ),
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
    foreign_read = foreign_notices_read(database, at)
    _note_missing_sectors(database, companies, at)
    results = {}
    for ticker in tickers:
        foreign, issuer = _foreign_inputs(issuers.get(ticker) or {}, periodic.get(ticker))
        inputs = {
            "exchange": companies[ticker]["exchange"] if ticker in companies else None,
            "issuer": issuer,
            "halted_on": halts.get(ticker),
            "delisting_on": delistings.get(ticker),
            "today": at.date(),
            "filed": periodic.get(ticker),
            # A foreign issuer's notices are 6-Ks read from their text: no notice is
            # known only once that reading has covered the whole window.
            "delistings_read": read and (foreign_read or not foreign),
        }
        results[ticker] = standards(**inputs)
        if facts is not None:
            facts[ticker] = inputs
    return results
