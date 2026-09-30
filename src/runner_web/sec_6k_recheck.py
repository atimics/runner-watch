"""Re-read archived 6-Ks of the last 90 days for exchange listing notices (#469).

A foreign issuer reports a delisting or deficiency notice on 6-K. That text is
only read as filings arrive, so 6-Ks stored before that are marked as ordinary
current reports, and the trading standard may have passed the issuer without
having seen its notice.

This reads the 6-K texts already archived in `source_documents` (it fetches
nothing) and reports which 6-Ks read as notices. It is a DRY RUN unless
`--apply` is given: then it marks those 6-Ks, and it records that the whole
window has been read only when every 6-K in it had archived text.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ratitrust.stock import DELISTING_DAYS
from runner_watch.edgar import LISTING_NOTICE_ITEM, filing_directory_url, is_listing_notice


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


def recheck_archived_6ks(database: Any, *, at: datetime, apply: bool = False) -> dict[str, Any]:
    """Report (and with `apply`, mark) archived 6-Ks whose text is a listing notice."""

    since = (at - timedelta(days=DELISTING_DAYS)).isoformat()
    rows = database.execute(
        "SELECT accession,ticker,filed_at,filing_url,items FROM sec_filings "
        "WHERE form LIKE ? AND filed_at>=? ORDER BY filed_at",
        ("6-K%", since),
    ).fetchall()
    notices: list[dict[str, str]] = []
    missing = 0
    already = 0
    for row in rows:
        if LISTING_NOTICE_ITEM in str(row["items"] or ""):
            already += 1
            continue
        text = _archived_text(database, str(row["filing_url"] or ""))
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
    arguments = parser.parse_args()
    from runner_web import db

    if arguments.database_path:
        if db.DATABASE_URL:
            parser.error("--database-path cannot be combined with DATABASE_URL")
        db.DATABASE_PATH = arguments.database_path
    with db.connection() as database:
        report = recheck_archived_6ks(database, at=datetime.now(UTC), apply=arguments.apply)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
