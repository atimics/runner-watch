"""Published U.S. macro readings and broad, completed-session equity breadth for Dash."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import urllib.request
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from starlette.concurrency import run_in_threadpool

from runner_watch.ingestion import MacroObservation, SourceBatch, SourceFetch
from runner_watch.massive_data import MassiveDailyCache, massive_cache_path
from runner_watch.xml_security import read_limited
from runner_web.data_health import recent_stock_closes
from runner_web.db import connection
from runner_web.ingestion import record_source_batch

LOG = logging.getLogger(__name__)
FRED = "https://fred.stlouisfed.org"
FRED_FEED = "us_macro"
BREADTH_FEED = "us_equity_breadth"
BREADTH_SERIES = "US_EQUITY_CLOSING_BREADTH"
BREADTH_SOURCE = "https://massive.com/docs/rest/stocks/aggregates/daily-market-summary"
FRED_REFRESH_SECONDS = 3600
BREADTH_REFRESH_SECONDS = 600
MAX_SERIES_BYTES = 64_000
CPI_LOOKBACK_DAYS = 550
CPI_SERIES = {"CPIAUCSL", "CPIAUCNS"}
SPECS = {
    "DFF": ("Effective federal funds rate", "Federal Reserve Board"),
    "DGS2": ("2-year Treasury constant maturity", "Federal Reserve Board"),
    "DGS10": ("10-year Treasury constant maturity", "Federal Reserve Board"),
    "AAA10Y": ("Moody’s Aaa corporate yield spread to 10-year Treasury", "Moody’s / St. Louis Fed"),
    "BAA10Y": ("Moody’s Baa corporate yield spread to 10-year Treasury", "Moody’s / St. Louis Fed"),
    "CPIAUCSL": ("Headline CPI, seasonally adjusted", "U.S. Bureau of Labor Statistics"),
    "CPIAUCNS": ("Headline CPI, not seasonally adjusted", "U.S. Bureau of Labor Statistics"),
}
MACRO_SECTIONS = ("rates", "inflation", "credit", "breadth")


def _download(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/csv",
            "User-Agent": "RunnerWatch/1.0 (+https://runners.rati.chat)",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return read_limited(response, max_bytes=MAX_SERIES_BYTES)


def _series_values(body: bytes, series: str, start: date, end: date) -> dict[date, float]:
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    headings = reader.fieldnames or []
    day_field = "observation_date" if "observation_date" in headings else "DATE"
    if day_field not in headings or series not in headings:
        raise ValueError(f"FRED CSV is missing {series} or the observation date")
    values = {}
    for row in reader:
        raw = (row[series] or "").strip()
        if raw in {"", "."}:
            continue
        observation = date.fromisoformat(row[day_field])
        value = float(raw)
        if start <= observation <= end and math.isfinite(value):
            values[observation] = value
    if not values:
        raise ValueError(f"FRED returned no observations for {series}")
    return values


def _observations(
    database: Any, source: str, feed: str, earliest: date, at: datetime
) -> dict[str, dict[date, dict[str, Any]]]:
    saved: dict[str, dict[date, dict[str, Any]]] = {}
    for row in database.execute(
        """
        SELECT * FROM (
            SELECT series_id,observation_date,vintage_date,value,payload_json,first_collected_at,
                ROW_NUMBER() OVER (
                    PARTITION BY series_id,observation_date ORDER BY vintage_date DESC
                ) AS revision_rank
            FROM macro_observations
            WHERE source=? AND feed=? AND observation_date BETWEEN ? AND ?
              AND first_collected_at<=?
        ) revisions WHERE revision_rank=1 ORDER BY observation_date DESC
        """,
        (source, feed, earliest.isoformat(), at.date().isoformat(), at.isoformat()),
    ).fetchall():
        saved.setdefault(row["series_id"], {})[date.fromisoformat(row["observation_date"])] = dict(
            row
        )
    return saved


def refresh_fred(*, at: datetime | None = None, download=_download) -> dict[str, str]:
    """Fetch each series independently and persist new values or revisions."""
    current = (at or datetime.now(UTC)).astimezone(UTC)
    end = current.date()
    earliest = (end - timedelta(days=CPI_LOOKBACK_DAYS)).replace(day=1)
    with connection() as database:
        saved = _observations(database, "fred", FRED_FEED, earliest, current)
    status = {}
    for series in SPECS:
        started = datetime.now(UTC)
        start = earliest if series in CPI_SERIES else end - timedelta(days=45)
        query = urlencode({"id": series, "cosd": start.isoformat(), "coed": end.isoformat()})
        url = f"{FRED}/graph/fredgraph.csv?{query}"
        try:
            body = download(url)
            values = _series_values(body, series, start, end)
        except (OSError, ValueError, csv.Error) as exc:
            fetch = SourceFetch.failure(
                source="fred",
                feed=FRED_FEED,
                locator=url,
                started_at=started,
                error=exc,
                metadata={"series_id": series},
            )
            record_source_batch(SourceBatch(fetch=fetch))
            status[series] = "unavailable"
            continue
        observations = tuple(
            MacroObservation(series, day, end, value)
            for day, value in values.items()
            if saved.get(series, {}).get(day, {}).get("value") != value
        )
        fetch = SourceFetch.success(
            source="fred",
            feed=FRED_FEED,
            locator=url,
            started_at=started,
            payload=body,
            content_type="text/csv",
            metadata={"series_id": series, "latest_observation": max(values).isoformat()},
        )
        record_source_batch(SourceBatch(fetch=fetch, macro_observations=observations))
        status[series] = "ok"
    return status


def refresh_breadth(*, at: datetime | None = None) -> int:
    """Publish aggregates from the existing full grouped-daily cache onto the shared DB."""
    current = (at or datetime.now(UTC)).astimezone(UTC)
    path = massive_cache_path()
    if not path.is_file():
        return 0
    closes = recent_stock_closes(current, count=7)
    with connection() as database:
        saved = _observations(database, "massive", BREADTH_FEED, closes[-1][0], current)
    history = saved.get(BREADTH_SERIES, {})
    cache = MassiveDailyCache(path)
    updates = []
    try:
        for (session, close), (previous, prev_close) in zip(closes, closes[1:], strict=False):
            counts = cache.closing_breadth(session, previous)
            if not counts:
                continue
            if not (
                close <= datetime.fromisoformat(counts["session_collected_at"]) <= current
                and prev_close
                <= datetime.fromisoformat(counts["prior_session_collected_at"])
                <= current
            ):
                continue
            payload = {
                **counts,
                "session_close_at": close.isoformat(),
                "previous_session_date": previous.isoformat(),
                "coverage_pct": round(
                    100 * counts["matched_symbols"] / counts["eligible_symbols"], 2
                ),
            }
            if session in history and json.loads(history[session]["payload_json"]) == payload:
                continue
            updates.append(
                MacroObservation(
                    BREADTH_SERIES,
                    session,
                    current.date(),
                    100 * counts["advancing"] / counts["matched_symbols"],
                    payload,
                )
            )
    finally:
        cache.close()
    if updates:
        fetch = SourceFetch.success(
            source="massive",
            feed=BREADTH_FEED,
            locator=BREADTH_SOURCE,
            started_at=current,
            payload={"sessions": [item.observation_date.isoformat() for item in updates]},
            content_type="application/json",
        )
        record_source_batch(SourceBatch(fetch=fetch, macro_observations=tuple(updates)))
    return len(updates)


def _fred_source(series: str) -> dict[str, str]:
    name, provider = SPECS[series]
    return {
        "series_id": series,
        "name": name,
        "source": f"{provider} via FRED",
        "source_url": f"{FRED}/series/{series}",
    }


def _daily(
    series: str, values: dict[date, dict[str, Any]], today: date, *, credit: bool = False
) -> dict[str, Any]:
    source = _fred_source(series)
    if not values:
        return {**source, "status": "unavailable"}
    latest, *older = values
    point = values[latest]
    previous = older[0] if older else None
    return {
        **source,
        "status": "stale" if (today - latest).days > 5 else "available",
        "observation_date": latest.isoformat(),
        "observation_age_days": (today - latest).days,
        "collected_at": point["first_collected_at"],
        "value": round(point["value"] * 100, 1) if credit else point["value"],
        "unit": "basis_points" if credit else "percent",
        "previous_observation_date": previous.isoformat() if previous else None,
        "change_bps": round((point["value"] - values[previous]["value"]) * 100, 1)
        if previous
        else None,
    }


def _inflation_rate(
    series: str, values: dict[date, dict[str, Any]], today: date, *, annual: bool = False
) -> dict[str, Any]:
    source = _fred_source(series)
    if not values:
        return {**source, "status": "unavailable"}
    latest = next(iter(values))

    def comparison(month: date) -> dict[str, Any]:
        baseline = (
            month.replace(year=month.year - 1)
            if annual
            else (month - timedelta(days=1)).replace(day=1)
        )
        point = values[month]
        reference = values.get(baseline, {}).get("value")
        change = (
            round(100 * (point["value"] / reference - 1), 2)
            if reference and reference > 0
            else None
        )
        return {
            "observation_month": month.strftime("%Y-%m"),
            "comparison_month": baseline.strftime("%Y-%m"),
            "collected_at": point["first_collected_at"],
            "change_pct": change,
        }

    newest = comparison(latest)
    fallback = None
    if newest["change_pct"] is None:
        fallback = next(
            (
                reading
                for month in values
                if month < latest and (reading := comparison(month))["change_pct"] is not None
            ),
            None,
        )
    month_age = (today.year - latest.year) * 12 + today.month - latest.month
    return {
        **source,
        **newest,
        "status": (
            "stale" if month_age > 2 else "partial" if newest["change_pct"] is None else "available"
        ),
        **({"last_comparable": fallback} if fallback else {}),
    }


def macro_snapshot(at: datetime | None = None, *, section: str | None = None) -> dict[str, Any]:
    current = (at or datetime.now(UTC)).astimezone(UTC)
    today = current.date()
    with connection() as database:
        earliest = (today - timedelta(days=CPI_LOOKBACK_DAYS)).replace(day=1)
        fred = _observations(database, "fred", FRED_FEED, earliest, current)
        breadth_history = _observations(
            database,
            "massive",
            BREADTH_FEED,
            today - timedelta(days=30),
            current,
        ).get(BREADTH_SERIES, {})
    rates = {
        "effective_fed_funds": _daily("DFF", fred.get("DFF", {}), today),
        "treasury_2y": _daily("DGS2", fred.get("DGS2", {}), today),
        "treasury_10y": _daily("DGS10", fred.get("DGS10", {}), today),
    }
    two, ten = rates["treasury_2y"], rates["treasury_10y"]
    if two.get("observation_date") and two["observation_date"] == ten.get("observation_date"):
        rates["curve_2s10s"] = {
            "status": ten["status"],
            "observation_date": ten["observation_date"],
            "value": round((ten["value"] - two["value"]) * 100, 1),
            "unit": "basis_points",
            "basis": "10-year Treasury minus 2-year Treasury",
            "source_urls": [two["source_url"], ten["source_url"]],
        }
    inflation = {
        "headline_month_over_month": _inflation_rate("CPIAUCSL", fred.get("CPIAUCSL", {}), today),
        "headline_year_over_year": _inflation_rate(
            "CPIAUCNS",
            fred.get("CPIAUCNS", {}),
            today,
            annual=True,
        ),
    }
    credit = {
        "aaa_treasury_spread": _daily("AAA10Y", fred.get("AAA10Y", {}), today, credit=True),
        "baa_treasury_spread": _daily("BAA10Y", fred.get("BAA10Y", {}), today, credit=True),
    }
    expected_date, expected_close = recent_stock_closes(current, count=1)[0]
    breadth: dict[str, Any] = {
        "status": "unavailable",
        "basis": "Provider-adjusted daily closes of U.S. non-OTC symbols that traded on both dates",
        "source": "Massive grouped daily bars",
        "source_url": BREADTH_SOURCE,
        "expected_session_date": expected_date.isoformat(),
        "expected_session_close_at": expected_close.isoformat(),
    }
    if breadth_history:
        latest, point = next(iter(breadth_history.items()))
        breadth.update(
            status="available" if latest == expected_date else "lagging",
            session_date=latest.isoformat(),
            collected_at=point["first_collected_at"],
            advancing_pct=round(point["value"], 2),
            **json.loads(point["payload_json"]),
        )
    sections = {"rates": rates, "inflation": inflation, "credit": credit, "breadth": breadth}
    selected = sections if section is None else {section: sections[section]}
    checks = [
        item["status"]
        for key, group in selected.items()
        for item in ([group] if key == "breadth" else group.values())
    ]
    status = (
        "available"
        if all(value == "available" for value in checks)
        else "unavailable"
        if all(value == "unavailable" for value in checks)
        else "partial"
    )
    return {
        "known": status != "unavailable",
        "status": status,
        "scope": "United States",
        "checked_at": current.isoformat(),
        **selected,
    }


async def collect_macro(refresh, interval_seconds: int) -> None:
    while True:
        try:
            await run_in_threadpool(refresh)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Macro collection failed in %s", refresh.__name__)
        await asyncio.sleep(interval_seconds)
