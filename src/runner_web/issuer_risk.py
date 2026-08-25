from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any


def _latest(rows: list[dict[str, Any]], concept: str) -> dict[str, Any] | None:
    matches = [row for row in rows if row["concept"] == concept]
    return max(matches, key=lambda row: (row["filed_at"], row["period_end"])) if matches else None


def _value(row: dict[str, Any] | None) -> float | None:
    return float(row["value"]) if row is not None else None


def build_issuer_risk_context(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "issuer_data_available": False,
            "cash": None,
            "cash_runway_months": None,
            "shares_growth_pct": None,
            "current_ratio": None,
            "debt_to_cash": None,
            "facts_filed_at": None,
        }

    cash = _value(_latest(rows, "cash"))
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
    debt_to_cash = debt_total / cash if debt_total is not None and cash and cash > 0 else None

    operating = _latest(rows, "operating_cash_flow")
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
            shares_growth = (
                float(latest_shares["value"]) / float(comparison["value"]) - 1
            ) * 100

    return {
        "issuer_data_available": True,
        "cash": cash,
        "cash_runway_months": round(runway, 1) if runway is not None else None,
        "shares_growth_pct": round(shares_growth, 1) if shares_growth is not None else None,
        "current_ratio": round(current_ratio, 2) if current_ratio is not None else None,
        "debt_to_cash": round(debt_to_cash, 2) if debt_to_cash is not None else None,
        "facts_filed_at": max(str(row["filed_at"]) for row in rows),
    }


def issuer_risk_contexts(database: Any, tickers: list[str]) -> dict[str, dict[str, Any]]:
    unique = list(dict.fromkeys(ticker.upper() for ticker in tickers if ticker))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    rows = database.execute(
        f"""
        SELECT c.ticker,f.concept,f.value,f.unit,f.period_start,f.period_end,
               f.filed_at,f.form,f.source_tag
        FROM sec_companies c
        LEFT JOIN issuer_facts f ON f.cik=c.cik
        WHERE c.ticker IN ({placeholders})
        ORDER BY c.ticker,f.filed_at DESC,f.period_end DESC
        """,  # noqa: S608
        unique,
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    known = set(unique)
    for raw in rows:
        row = dict(raw)
        ticker = str(row["ticker"])
        known.discard(ticker)
        if row.get("concept"):
            grouped[ticker].append(row)
        else:
            grouped.setdefault(ticker, [])
    for ticker in known:
        grouped.setdefault(ticker, [])
    return {ticker: build_issuer_risk_context(facts) for ticker, facts in grouped.items()}
