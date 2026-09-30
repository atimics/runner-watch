"""Re-read archived 6-Ks of the last 90 days for exchange listing notices (#469).

A foreign issuer reports a delisting or deficiency notice on 6-K. That text is
only read as filings arrive, so 6-Ks stored before that are marked as ordinary
current reports, and the trading standard may have passed the issuer without
having seen its notice.

This reads the 6-K texts already archived in `source_documents` and reports
which 6-Ks read as notices. By default it fetches nothing. It is a DRY RUN
unless `--apply` is given: then it marks those 6-Ks, and it records that the
whole window has been read only when every 6-K in it had text.

`--fetch` is optional. It downloads the text of 6-Ks that were never archived
from SEC EDGAR (the SEC_USER_AGENT env, at most 2 requests a second, two
requests per 6-K: the folder index and the document), at most `--max-fetch`
6-Ks (default 50). What is written:

- `--fetch` alone writes NOTHING. The text is read in memory and dropped.
- `--fetch --apply` also archives each fetched text in `source_documents`
  (with the usual source fetch log rows), then marks notices as `--apply`
  does. A 6-K whose fetch fails, or that is over the limit, stays unread, so
  the window is not recorded as read and a later run can go on.

Run against a copy or a local database first:

    uv run python -m runner_web.sec_6k_recheck --database-path /path/to/copy.db
    uv run python -m runner_web.sec_6k_recheck --database-path /path/to/copy.db --apply

The trading standard's history (`ratification_records`) gains a new entry for
each stock whose result changes the next time the standard is computed, so a
card that showed "met" and now shows "not met" is corrected in the open.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ratitrust.stock import DELISTING_DAYS
from runner_watch.edgar import (
    LISTING_NOTICE_ITEM,
    SEC_USER_AGENT,
    EdgarClient,
    EdgarFiling,
    filing_directory_url,
    is_listing_notice,
)

LOG = logging.getLogger(__name__)
DEFAULT_MAX_FETCH = 50
# Two requests per 6-K, so this is at most one 6-K a second. SEC allows 10 a second.
FETCH_REQUESTS_PER_SECOND = 2.0
# Given a filing's URL and accession, returns its primary document text or None.
FetchText = Callable[[str, str], str | None]


def _archived_text(database: Any, filing_url: str) -> str | None:
    """The archived primary document of a filing, or None when none is archived."""

    try:
        directory = filing_directory_url(filing_url)
    except ValueError:
        return None
    rows = database.execute(
        "SELECT source_url,content,content_encoding,content_hash FROM source_documents "
        "WHERE source='sec' AND source_url LIKE ? AND source_url NOT LIKE ? "
        "ORDER BY last_collected_at DESC",
        (directory + "/%", "%/index.json"),
    ).fetchall()
    for row in rows:
        body = bytes(row["content"])
        if row["content_encoding"] == "gzip":
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError):
                continue
        if hashlib.sha256(body).hexdigest() != str(row["content_hash"]):
            continue
        return body.decode("utf-8", errors="replace")
    return None


def edgar_text_fetcher(*, archive: bool) -> FetchText:
    """Fetch a 6-K's primary document from SEC EDGAR, slowly.

    With `archive` the client records each response in `source_documents`; without
    it nothing is written anywhere.
    """

    recorder = None
    if archive:
        from runner_web.ingestion import record_source_fetch

        recorder = record_source_fetch
    client = EdgarClient(
        user_agent=SEC_USER_AGENT,
        max_requests_per_second=FETCH_REQUESTS_PER_SECOND,
        fetch_recorder=recorder,
    )

    def fetch(filing_url: str, accession: str) -> str | None:
        filing = EdgarFiling(
            accession=accession,
            cik=0,
            form="6-K",
            title="",
            role="",
            filed_at="",
            filing_url=filing_url,
        )
        found = client.primary_filing_text(filing)
        return found[1] if found else None

    return fetch


def recheck_archived_6ks(
    database: Any,
    *,
    at: datetime,
    apply: bool = False,
    fetch_text: FetchText | None = None,
    max_fetch: int = DEFAULT_MAX_FETCH,
) -> dict[str, Any]:
    """Report (and with `apply`, mark) 6-Ks whose text is a listing notice.

    Text comes from the archive. When `fetch_text` is given, a 6-K with no archived
    text is fetched with it, for at most `max_fetch` 6-Ks; without it nothing is
    fetched.
    """

    since = (at - timedelta(days=DELISTING_DAYS)).isoformat()
    rows = database.execute(
        "SELECT accession,ticker,filed_at,filing_url,items FROM sec_filings "
        "WHERE form LIKE ? AND filed_at>=? ORDER BY filed_at",
        ("6-K%", since),
    ).fetchall()
    notices: list[dict[str, str]] = []
    missing = 0
    already = 0
    fetched = 0
    fetch_failed = 0
    for row in rows:
        if LISTING_NOTICE_ITEM in str(row["items"] or ""):
            already += 1
            continue
        text = _archived_text(database, str(row["filing_url"] or ""))
        if text is None and fetch_text is not None and fetched < max_fetch:
            fetched += 1
            try:
                text = fetch_text(str(row["filing_url"] or ""), str(row["accession"]))
            except Exception:
                LOG.warning("Could not fetch 6-K %s", row["accession"], exc_info=True)
                text = None
            if text is None:
                fetch_failed += 1
        if text is None:
            missing += 1
        elif is_listing_notice(text):
            notices.append(
                {
                    "accession": row["accession"],
                    "ticker": row["ticker"],
                    "filed_at": str(row["filed_at"])[:10],
                }
            )
    report: dict[str, Any] = {
        "dry_run": not apply,
        "since": since[:10],
        "six_ks": len(rows),
        "already_marked": already,
        "text_not_archived": missing,
        "fetched": fetched,
        "fetch_failed": fetch_failed,
        "would_mark" if not apply else "marked": notices,
        "tickers": sorted({notice["ticker"] for notice in notices}),
        "window_marked_as_read": False,
    }
    if not apply:
        return report
    stamp = at.isoformat()
    for notice in notices:
        database.execute(
            "UPDATE sec_filings SET items=?,kind=?,sentiment=?,score=?,updated_at=? "
            "WHERE accession=?",
            (
                LISTING_NOTICE_ITEM,
                "Exchange listing notice",
                "risk",
                76.0,
                stamp,
                notice["accession"],
            ),
        )
    if missing == 0:
        from runner_web.sec_delistings import mark_foreign_notices_read_from

        mark_foreign_notices_read_from(database, at - timedelta(days=DELISTING_DAYS))
        report["window_marked_as_read"] = True
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-path", type=Path)
    parser.add_argument("--apply", action="store_true", help="write changes (default: report only)")
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="download 6-Ks with no archived text from SEC EDGAR; with --apply the texts are "
        "also archived (default: fetch nothing)",
    )
    parser.add_argument(
        "--max-fetch",
        type=int,
        default=DEFAULT_MAX_FETCH,
        help=f"most 6-Ks to download with --fetch (default {DEFAULT_MAX_FETCH})",
    )
    arguments = parser.parse_args()
    if arguments.max_fetch < 0:
        parser.error("--max-fetch cannot be negative")
    from runner_web import db

    if arguments.database_path:
        if db.DATABASE_URL:
            parser.error("--database-path cannot be combined with DATABASE_URL")
        db.DATABASE_PATH = arguments.database_path
    with db.connection() as database:
        report = recheck_archived_6ks(
            database,
            at=datetime.now(UTC),
            apply=arguments.apply,
            fetch_text=edgar_text_fetcher(archive=arguments.apply) if arguments.fetch else None,
            max_fetch=arguments.max_fetch,
        )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
