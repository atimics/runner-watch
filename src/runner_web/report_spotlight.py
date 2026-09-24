"""Freeze one editorial company profile from the report's saved evidence."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from runner_web.stock_indicator import stock_indicator


def number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def source_url(value: Any) -> str | None:
    url = str(value or "")
    try:
        parsed = urlsplit(url)
        return url if parsed.scheme == "https" and parsed.hostname else None
    except ValueError:
        return None


def _interest(row: dict[str, Any]) -> list[dict[str, Any]]:
    move = number(row.get("change_pct"))
    volume = number(row.get("relative_volume"))
    signals = row.get("signals") or []
    risks = row.get("risks") or []
    return [
        {"label": "Price move", "points": min(abs(move or 0), 25) * 1.6, "max": 40},
        {"label": "Volume", "points": min(max((volume or 0) - 1, 0), 5) * 5, "max": 25},
        {"label": "Signals", "points": min(len(signals), 4) * 5, "max": 20},
        {"label": "Risk context", "points": min(len(risks), 3) * 5, "max": 15},
    ]


def select_spotlight(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Rank both directions, with an alphabetical tie break for repeatable editions."""
    candidates = [row for row in rows if (number(row.get("price")) or 0) > 0]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda row: (-sum(part["points"] for part in _interest(row)), str(row["ticker"])),
    )


def freeze_spotlight(
    database: Any, rows: list[dict[str, Any]], as_of: str, captured_at: str
) -> dict[str, Any] | None:
    def session_day(value: Any) -> Any:
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(ZoneInfo("America/New_York")).date()

    day = session_day(as_of)
    eligible = [row for row in rows if day and session_day(row.get("quote_time")) == day]
    selected = select_spotlight(eligible)
    if selected is None:
        return None
    ticker = str(selected["ticker"])
    company_row = database.execute(
        "SELECT * FROM sec_companies WHERE ticker=? AND refreshed_at<=? "
        "ORDER BY refreshed_at DESC,cik LIMIT 1",
        (ticker, captured_at),
    ).fetchone()
    company = dict(company_row) if company_row else {}
    cik = company.get("cik")
    filings = database.execute(
        "SELECT form,title,filed_at,filing_url FROM sec_filings "
        "WHERE ticker=? AND filed_at<=? AND created_at<=? "
        "ORDER BY filed_at DESC,accession DESC LIMIT 4",
        (ticker, as_of, captured_at),
    ).fetchall()
    facts = (
        database.execute(
            "SELECT concept,value,unit,period_start,period_end,filed_at,accession "
            "FROM issuer_facts WHERE cik=? AND filed_at<=? AND first_collected_at<=? "
            "AND concept IN ('cash','debt_total','shares_outstanding','operating_cash_flow') "
            "ORDER BY period_end DESC,filed_at DESC,id DESC LIMIT 120",
            (cik, as_of, captured_at),
        ).fetchall()
        if cik
        else []
    )
    labels = {
        "cash": "Cash",
        "debt_total": "Total debt",
        "shares_outstanding": "Shares",
        "operating_cash_flow": "Operating cash flow",
    }
    saved_facts: dict[str, Any] = {}
    for raw in facts:
        fact = dict(raw)
        key = fact["concept"]
        value = number(fact["value"])
        expected_unit = "shares" if key == "shares_outstanding" else "USD"
        if key in saved_facts or value is None or fact["unit"] != expected_unit:
            continue
        accession = str(fact["accession"])
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession.replace('-', '')}/{accession}-index.html"
        )
        saved_facts[key] = {**fact, "label": labels[key], "source_url": url}
    parts = _interest(selected)
    move = number(selected.get("change_pct"))
    volume = number(selected.get("relative_volume"))
    reasons = []
    if move is not None:
        reasons.append(f"{move:+.1f}% at the saved checkpoint")
    if volume is not None:
        reasons.append(f"{volume:.1f}× relative volume")
    if selected.get("signals"):
        reasons.append(f"{len(selected['signals'])} saved signals")
    if selected.get("risks"):
        reasons.append(f"{len(selected['risks'])} risk flags")
    industry = company.get("sic_description")
    if str(company.get("sector_refreshed_at") or "") > captured_at:
        industry = None
    return {
        "version": "report-interest-v1",
        "ticker": ticker,
        "snapshot": dict(selected),
        "as_of": as_of,
        "captured_at": captured_at,
        "universe": len(rows),
        "eligible": len(eligible),
        "selection_parts": parts,
        "reason": " · ".join(reasons) or "The leading eligible name in this saved scan.",
        "company": {
            "name": company.get("name") or ticker,
            "industry": industry,
            "exchange": company.get("exchange"),
            "cik": cik,
            "source_url": f"https://www.sec.gov/edgar/browse/?CIK={int(cik)}" if cik else None,
            "as_of": company.get("refreshed_at"),
        },
        "facts": list(saved_facts.values()),
        "filings": [{**dict(row), "filing_url": source_url(row["filing_url"])} for row in filings],
    }


def _compact(value: Any, unit: str) -> str:
    amount = number(value)
    if amount is None:
        return "Awaiting data"
    prefix = ("-" if amount < 0 else "") + ("$" if unit == "USD" else "")
    amount = abs(amount)
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(amount) >= divisor:
            return f"{prefix}{amount / divisor:,.1f}{suffix}"
    return f"{prefix}{amount:,.0f}"


def decorate_edition(report: dict[str, Any]) -> None:
    """Only saved fields feed historical visuals; page reads require no new quotes."""
    post = report["report_type"] == "post_market"
    for leader in report["leaders"]:
        leader["indicator"] = stock_indicator(leader)
    spotlight = report.get("spotlight")
    if isinstance(spotlight, dict) and isinstance(spotlight.get("snapshot"), dict):
        spotlight["snapshot"]["indicator"] = stock_indicator(spotlight["snapshot"])
        for fact in spotlight.get("facts", []):
            fact["display"] = _compact(fact.get("value"), fact.get("unit", ""))
    else:
        spotlight = None
        report["spotlight"] = None
    hero = spotlight["snapshot"] if post and spotlight else next(iter(report["leaders"]), None)
    breadth = report["metrics"].get("closing_breadth") if post else report["metrics"]
    breadth = breadth if isinstance(breadth, dict) else None
    segments = []
    if breadth and (number(breadth.get("candidates")) or 0) > 0:
        total = int(breadth["candidates"])
        green = max(0, min(total, int(breadth.get("green") or 0)))
        red = max(0, min(total - green, int(breadth.get("red") or 0)))
        for label, count, tone in (
            ("Up", green, "up"),
            ("Down", red, "down"),
            ("Flat / unpriced", total - green - red, "flat"),
        ):
            segments.append(
                {
                    "label": label,
                    "count": count,
                    "tone": tone,
                    "percent": round(count / total * 100, 3),
                }
            )
    try:
        date_label = datetime.fromisoformat(report["report_day"]).strftime("%A, %B %-d")
    except ValueError:
        date_label = report["report_day"]
    report["edition"] = {
        "title": "The closing story" if post else "The opening watch",
        "hero": hero,
        "date_label": date_label,
        "breadth": segments,
        "breadth_label": "Closing scan" if post else "Pre-market scan",
        "scope": "Stock of the day" if post and spotlight else "Watch board leader",
    }
