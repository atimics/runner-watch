"""Delisting notices (8-K item 3.01) for the last 90 days, from EDGAR full-text search.

The EDGAR current feed carries item numbers only for filings read since it
was parsed for them, so the trading standard would read "no notice" for any
company whose notice came earlier. A daily sweep of EDGAR full-text search,
which returns each 8-K's items, fills the window: a few requests a day.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ratitrust.stock import DELISTING_DAYS, DELISTING_SWEEP_FRESH_HOURS
from runner_watch.edgar import SEC_USER_AGENT, classify_filing
from runner_web.db import connection

LOG = logging.getLogger(__name__)
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index?"
WINDOW = timedelta(days=DELISTING_DAYS)
PAGE = 100
MAX_PAGES = 20
RETRY_PAUSE = 5.0
STATE_KEY = "sec_delisting_sweep"
# A sweep older than this no longer vouches for the window (the rules' own figure).
FRESH = timedelta(hours=DELISTING_SWEEP_FRESH_HOURS)
TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)")
Download = Callable[[str, float], bytes]


def _download(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(8 * 1024 * 1024)


def _fetch(download: Download, url: str) -> bytes:
    """One search, retried once after a pause: the search service fails now and then."""

    try:
        return download(url, 20.0)
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            raise
        time.sleep(RETRY_PAUSE)
        return download(url, 20.0)


def search_notices(*, at: datetime, download: Download = _download) -> list[dict[str, Any]]:
    """Every 8-K or 8-K/A in the window whose items include 3.01."""

    start = (at - WINDOW).date().isoformat()
    notices: dict[str, dict[str, Any]] = {}
    # One form per search: a list of forms narrows the search instead of widening it.
    for form in ("8-K", "8-K/A"):
        offset = 0
        for _ in range(MAX_PAGES):
            query = urllib.parse.urlencode(
                {
                    "q": '"Item 3.01"',
                    "forms": form,
                    "dateRange": "custom",
                    "startdt": start,
                    "enddt": at.date().isoformat(),
                    "from": offset,
                }
            )
            body = json.loads(_fetch(download, SEARCH_URL + query))
            hits = body["hits"]["hits"]
            for hit in hits:
                notice = _notice(hit["_source"])
                if notice:
                    notices[notice["accession"]] = notice
            offset += len(hits)
            if not hits or offset >= int(body["hits"]["total"]["value"]):
                break
    return list(notices.values())


def _notice(source: dict[str, Any]) -> dict[str, Any] | None:
    items = [str(item) for item in source.get("items") or []]
    accession = str(source.get("adsh") or "")
    if "3.01" not in items or not accession or not source.get("ciks"):
        return None
    names = source.get("display_names") or [""]
    match = TICKER_RE.search(str(names[0]))
    return {
        "accession": accession,
        "cik": int(source["ciks"][0]),
        "company": re.sub(r"\s*\(.*$", "", str(names[0])).strip(),
        "ticker": match.group(1) if match else "",
        "form": str(source.get("form") or "8-K").upper(),
        "filed_at": f"{source['file_date']}T00:00:00+00:00",
        "items": ",".join(sorted(set(items))),
    }


def record_notices(database: Any, notices: list[dict[str, Any]], *, at: datetime) -> int:
    """Store each notice, or add its items to a filing already stored without them."""

    classification = classify_filing("8-K", None)
    stored = 0
    for notice in notices:
        company = database.execute(
            "SELECT ticker,name FROM sec_companies WHERE cik=? ORDER BY ticker LIMIT 1",
            (notice["cik"],),
        ).fetchone()
        ticker = str(company["ticker"]) if company else notice["ticker"]
        if not ticker:
            continue  # no listed ticker: no stock of ours to rate
        database.execute(
            "UPDATE sec_filings SET items=?,updated_at=? "
            "WHERE accession=? AND (items IS NULL OR items='')",
            (notice["items"], at.isoformat(), notice["accession"]),
        )
        folder = notice["accession"].replace("-", "")
        database.execute(
            """
            INSERT OR IGNORE INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                filing_url,created_at,updated_at,items
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                notice["accession"],
                notice["cik"],
                ticker,
                str(company["name"]) if company else notice["company"],
                notice["form"],
                classification["kind"],
                classification["sentiment"],
                float(classification["score"]),
                f"{notice['form']} - Notice of Delisting (Item 3.01)",
                notice["filed_at"],
                f"https://www.sec.gov/Archives/edgar/data/{notice['cik']}/{folder}/"
                f"{notice['accession']}-index.htm",
                at.isoformat(),
                at.isoformat(),
                notice["items"],
            ),
        )
        stored += 1
    return stored


def refresh_delisting_notices(
    *, at: datetime | None = None, download: Download = _download
) -> dict[str, Any]:
    at = at or datetime.now(UTC)
    notices = search_notices(at=at, download=download)
    with connection() as database:
        stored = record_notices(database, notices, at=at)
        state = {"checked_at": at.isoformat(), "notices": len(notices), "stored": stored}
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (STATE_KEY, json.dumps(state), at.isoformat()),
        )
    return state


def notices_read(database: Any, at: datetime) -> bool:
    """Whether a sweep recent enough to vouch for the 90-day window has run."""

    row = database.execute("SELECT value FROM worker_state WHERE key=?", (STATE_KEY,)).fetchone()
    try:
        checked = datetime.fromisoformat(json.loads(row["value"])["checked_at"])
    except (TypeError, KeyError, ValueError):
        return False
    return at - checked <= FRESH


# A foreign issuer's notice is a 6-K, read from its text as the filing arrives
# (`runner_web.intelligence`). Until that reading has covered the whole 90-day
# window, "no notice" is not known for it: the standard says "not checked yet".
FOREIGN_STATE_KEY = "sec_6k_notices_read_from"


def mark_foreign_notices_read_from(database: Any, day: datetime) -> None:
    """Record the day from which every 6-K's text has been read for notices.

    An earlier day already on record is kept, so a later reading never shortens
    the window that was covered.
    """

    row = database.execute(
        "SELECT value FROM worker_state WHERE key=?", (FOREIGN_STATE_KEY,)
    ).fetchone()
    if row:
        try:
            if datetime.fromisoformat(json.loads(row["value"])["from"]) <= day:
                return
        except (TypeError, KeyError, ValueError):
            pass
    database.execute(
        "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (FOREIGN_STATE_KEY, json.dumps({"from": day.isoformat()}), day.isoformat()),
    )


def foreign_notices_read(database: Any, at: datetime) -> bool:
    """Whether 6-K texts have been read for the whole 90-day window ending `at`."""

    row = database.execute(
        "SELECT value FROM worker_state WHERE key=?", (FOREIGN_STATE_KEY,)
    ).fetchone()
    try:
        start = datetime.fromisoformat(json.loads(row["value"])["from"])
    except (TypeError, KeyError, ValueError):
        return False
    return start <= at - WINDOW
