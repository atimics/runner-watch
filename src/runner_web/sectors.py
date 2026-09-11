"""Company sectors, backfilled from SEC submissions.

The company map gives a ticker, a name and an exchange, which is enough to scan
but not enough to answer "how is biotech doing". SEC publishes a standard
industrial classification per filer in its submissions endpoint, so this fills
that column in a little at a time rather than demanding one large import.

Sectors are grouped from the SIC code itself. The four digit code is
hierarchical, so the leading digits give a division without needing a second
source, and the description SEC supplies is kept alongside for display.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.db import connection

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SECTOR_REFRESH_DAYS = max(1, int(os.getenv("SECTOR_REFRESH_DAYS", "90")))
SECTOR_BACKFILL_BATCH = max(1, int(os.getenv("SECTOR_BACKFILL_BATCH", "25")))

# SIC divisions, from the leading digits of the code.
_DIVISIONS: tuple[tuple[int, int, str], ...] = (
    (100, 999, "Agriculture"),
    (1000, 1499, "Mining and energy"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transport and utilities"),
    (5000, 5199, "Wholesale"),
    (5200, 5999, "Retail"),
    (6000, 6799, "Finance and real estate"),
    (7000, 8999, "Services"),
    (9100, 9999, "Public administration"),
)
# Codes worth naming on their own, because they are what this board is made of.
_NAMED: dict[int, str] = {
    2834: "Biotech and pharma",
    2836: "Biotech and pharma",
    8731: "Biotech and pharma",
    3674: "Semiconductors",
    7372: "Software",
    7370: "Software",
    7371: "Software",
    6770: "Blank checks and shells",
    1311: "Oil and gas",
    1040: "Mining and energy",
}


def sector_for(sic: Any) -> str | None:

    """Group a SIC code into something a person would actually say."""

    try:
        code = int(str(sic).strip())
    except (TypeError, ValueError):
        return None
    if code <= 0:
        return None
    if code in _NAMED:
        return _NAMED[code]
    for low, high, label in _DIVISIONS:
        if low <= code <= high:
            return label
    return None


def _stale_before(now: datetime) -> str:
    return (now - timedelta(days=SECTOR_REFRESH_DAYS)).isoformat()


def companies_missing_sectors(database: Any, now: datetime, limit: int) -> list[dict[str, Any]]:

    """Filers on the board that have no sector yet, or a stale one."""

    rows = database.execute(
        """
        SELECT cik,ticker FROM sec_companies
        WHERE cik IS NOT NULL
          AND (sector_refreshed_at IS NULL OR sector_refreshed_at<?)
        ORDER BY sector_refreshed_at IS NOT NULL,ticker
        LIMIT ?
        """,
        (_stale_before(now), limit),
    ).fetchall()
    return [dict(row) for row in rows]


def save_sector(
    database: Any,
    cik: int,
    sic: str | None,
    description: str | None,
    now: datetime,
) -> None:
    database.execute(
        """
        UPDATE sec_companies
        SET sic=?,sic_description=?,sector_refreshed_at=? WHERE cik=?
        """,
        (sic, description, now.isoformat(), cik),
    )


def refresh_company_sectors(
    client: Any = None,
    *,
    at: datetime | None = None,
    limit: int | None = None,
) -> dict[str, int]:

    """Fill in sectors for a few filers.

    SEC asks for a slow, identified client, so this takes a small batch each
    pass and leans on the shared EDGAR client's own throttle rather than
    fetching everything at once.
    """

    now = at or datetime.now(UTC)
    batch = SECTOR_BACKFILL_BATCH if limit is None else max(1, limit)
    if client is None:
        from runner_watch.edgar import EdgarClient

        client = EdgarClient()
    with connection() as database:
        pending = companies_missing_sectors(database, now, batch)
    counts = {"checked": 0, "stored": 0, "missing": 0, "failed": 0}
    for row in pending:
        try:
            cik = int(row["cik"])
        except (TypeError, ValueError):
            continue
        counts["checked"] += 1
        try:
            payload = client.get_json(SUBMISSIONS_URL.format(cik=cik))
        except Exception:
            counts["failed"] += 1
            continue
        sic = str(payload.get("sic") or "").strip() or None
        description = str(payload.get("sicDescription") or "").strip() or None
        with connection() as database:
            save_sector(database, cik, sic, description, now)
        counts["stored" if sic else "missing"] += 1
    return counts


def sector_board(database: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:

    """Group board rows by sector, busiest first.

    Rows without a known sector are carried in their own group rather than
    dropped, so a reader can tell "no filers in biotech today" apart from
    "we have not looked up what these are".
    """

    tickers = sorted({str(row.get("ticker") or "") for row in rows if row.get("ticker")})
    if not tickers:
        return []
    placeholders = ",".join("?" for _ in tickers)
    lookup = {
        str(found["ticker"]): found
        for found in database.execute(
            f"SELECT ticker,sic,sic_description FROM sec_companies "
            f"WHERE ticker IN ({placeholders})",
            tickers,
        ).fetchall()
    }
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        company = lookup.get(str(row.get("ticker") or ""))
        label = sector_for(company["sic"]) if company else None
        key = label or "Unclassified"
        bucket = grouped.setdefault(
            key,
            {"sector": key, "count": 0, "tickers": [], "changes": []},
        )
        bucket["count"] += 1
        if len(bucket["tickers"]) < 6:
            bucket["tickers"].append(str(row.get("ticker") or ""))
        change = row.get("change_pct")
        if isinstance(change, (int, float)):
            bucket["changes"].append(float(change))
    board = []
    for bucket in grouped.values():
        changes = bucket.pop("changes")
        bucket["average_change_pct"] = (
            round(sum(changes) / len(changes), 2) if changes else None
        )
        board.append(bucket)
    board.sort(key=lambda item: (-item["count"], item["sector"]))
    return board
