from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

# The forms and the financial-company range are the RATi Rules' own: one
# place decides them, and the risk check, ratification and Dash read it.
from ratitrust.stock import (
    FINANCIAL_SIC,
    FOREIGN_ANNUAL_FORMS,
    FOREIGN_FORMS,
    PERIODIC_FORMS,
    is_financial,
    is_foreign_issuer,
)

__all__ = [
    "FINANCIAL_SIC",
    "FOREIGN_ANNUAL_FORMS",
    "FOREIGN_FORMS",
    "PERIODIC_FORMS",
    "is_financial",
    "is_foreign_issuer",
]


def _latest(rows: list[dict[str, Any]], concept: str) -> dict[str, Any] | None:
    matches = [row for row in rows if row["concept"] == concept]
    return max(matches, key=lambda row: (row["filed_at"], row["period_end"])) if matches else None


def _value(row: dict[str, Any] | None) -> float | None:
    return float(row["value"]) if row is not None else None


def build_issuer_risk_context(rows: list[dict[str, Any]], sic: Any = None) -> dict[str, Any]:
    """Issuer facts and what they mean, decided once for every reader.

    For a financial company the runway is not applicable: it is None with
    `runway_applies` False, so no reader can treat it as a burn warning.
    """

    context = _raw_issuer_context(rows)
    financial = is_financial(sic)
    context.update(sic=str(sic).strip() if sic else None, financial=financial)
    context["runway_applies"] = not financial
    if financial:
        context["cash_runway_months"] = None
    return context


def _raw_issuer_context(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "issuer_data_available": False,
            "cash": None,
            "cash_runway_months": None,
            "shares_growth_pct": None,
            "current_ratio": None,
            "debt_to_cash": None,
            "facts_filed_at": None,
            "operating_cash_flow": None,
            "periodic_filed_at": None,
            "periodic_form": None,
            "foreign_issuer": False,
            "reporting_currency": None,
        }

    operating = _latest(rows, "operating_cash_flow")
    # An IFRS filer may report in its own currency: cash, burn and debt are only
    # compared within one unit, and cash is shown in dollars only when it is.
    currency = str((operating or _latest(rows, "cash") or {}).get("unit") or "USD")
    cash_row = _latest([row for row in rows if row.get("unit", "USD") == currency], "cash")
    cash = _value(cash_row)
    assets_current = _value(_latest(rows, "assets_current"))
    liabilities_current = _value(_latest(rows, "liabilities_current"))
    debt_total = _value(_latest(rows, "debt_total"))
    debt_current = _value(_latest(rows, "debt_current")) or 0.0
    debt_noncurrent = _value(_latest(rows, "debt_noncurrent")) or 0.0
    if debt_total is None and (debt_current or debt_noncurrent):
        debt_total = debt_current + debt_noncurrent

    current_ratio = (
        assets_current / liabilities_current
        if assets_current is not None and liabilities_current and liabilities_current > 0
        else None
    )
    debt_to_cash = (
        debt_total / cash
        if debt_total is not None and cash and cash > 0 and currency == "USD"
        else None
    )

    runway = None
    if cash is not None and cash >= 0 and operating is not None and float(operating["value"]) < 0:
        start_text = operating.get("period_start")
        try:
            start = date.fromisoformat(str(start_text)) if start_text else None
            end = date.fromisoformat(str(operating["period_end"]))
        except ValueError:
            start = None
            end = date.today()
        duration_months = max(1.0, (end - start).days / 30.44) if start else 3.0
        monthly_burn = -float(operating["value"]) / duration_months
        runway = cash / monthly_burn if monthly_burn > 0 else None

    share_rows = [row for row in rows if row["concept"] == "shares_outstanding"]
    by_period: dict[str, dict[str, Any]] = {}
    for row in share_rows:
        current = by_period.get(str(row["period_end"]))
        if current is None or str(row["filed_at"]) > str(current["filed_at"]):
            by_period[str(row["period_end"])] = row
    ordered_shares = sorted(
        by_period.values(), key=lambda row: str(row["period_end"]), reverse=True
    )
    shares_growth = None
    if ordered_shares:
        latest_shares = ordered_shares[0]
        try:
            latest_end = date.fromisoformat(str(latest_shares["period_end"]))
        except ValueError:
            latest_end = date.today()
        comparison = next(
            (
                row
                for row in ordered_shares[1:]
                if date.fromisoformat(str(row["period_end"])) <= latest_end - timedelta(days=270)
            ),
            None,
        )
        if comparison and float(comparison["value"]) > 0:
            shares_growth = (float(latest_shares["value"]) / float(comparison["value"]) - 1) * 100

    return {
        "issuer_data_available": True,
        "cash": cash if currency == "USD" else None,
        "reporting_currency": currency,
        "cash_runway_months": round(runway, 1) if runway is not None else None,
        "shares_growth_pct": round(shares_growth, 1) if shares_growth is not None else None,
        "current_ratio": round(current_ratio, 2) if current_ratio is not None else None,
        "debt_to_cash": round(debt_to_cash, 2) if debt_to_cash is not None else None,
        "facts_filed_at": max(str(row["filed_at"]) for row in rows),
        # Positive or zero: the company is not burning cash, so runway is not the question.
        "operating_cash_flow": _value(operating),
        # The latest quarterly or annual report behind these facts.
        **_latest_periodic(rows),
    }


def _latest_periodic(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The latest quarterly or annual report, domestic or foreign.

    A foreign private issuer's annual 20-F or 40-F is its periodic report; the
    freshness window for it is decided by the reader (ratification).
    """

    forms = {str(row.get("form") or "").upper() for row in rows}
    reports = [
        row
        for row in rows
        if str(row.get("form") or "").upper() in PERIODIC_FORMS | FOREIGN_ANNUAL_FORMS
    ]
    latest = max(reports, key=lambda row: str(row["filed_at"])) if reports else None
    return {
        "periodic_filed_at": str(latest["filed_at"]) if latest else None,
        "periodic_form": str(latest["form"]).upper() if latest else None,
        "foreign_issuer": is_foreign_issuer(forms),
    }


def issuer_risk_contexts(database: Any, tickers: list[str]) -> dict[str, dict[str, Any]]:
    unique = list(dict.fromkeys(ticker.upper() for ticker in tickers if ticker))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    rows = database.execute(
        f"""
        SELECT c.ticker,c.sic,f.concept,f.value,f.unit,f.period_start,f.period_end,
               f.filed_at,f.form,f.source_tag
        FROM sec_companies c
        LEFT JOIN issuer_facts f ON f.cik=c.cik
        WHERE c.ticker IN ({placeholders})
        ORDER BY c.ticker,f.filed_at DESC,f.period_end DESC
        """,
        unique,
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sics: dict[str, Any] = {}
    known = set(unique)
    for raw in rows:
        row = dict(raw)
        ticker = str(row["ticker"])
        known.discard(ticker)
        sics[ticker] = sics.get(ticker) or row.get("sic")
        if row.get("concept"):
            grouped[ticker].append(row)
        else:
            grouped.setdefault(ticker, [])
    for ticker in known:
        grouped.setdefault(ticker, [])
    return {
        ticker: build_issuer_risk_context(facts, sics.get(ticker))
        for ticker, facts in grouped.items()
    }
