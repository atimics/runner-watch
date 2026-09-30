"""The one-off re-read of archived 6-Ks (#469): a dry run unless told to apply."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

import pytest

from runner_web.sec_6k_recheck import recheck_archived_6ks
from runner_web.sec_delistings import foreign_notices_read

AT = datetime(2026, 9, 27, 12, tzinfo=UTC)
NOTICE = b"<p>The Company is not in compliance with Nasdaq Listing Rule 5550(a)(2).</p>"
ROUTINE = b"<p>Interim results for the six months ended June 30.</p>"


def _url(number: int) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/7/{number}/{number}-index.htm"


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE sec_filings (
            accession TEXT, ticker TEXT, form TEXT, items TEXT, kind TEXT, sentiment TEXT,
            score REAL, filed_at TEXT, filing_url TEXT, updated_at TEXT
        );
        CREATE TABLE source_documents (
            source TEXT, source_url TEXT, content_hash TEXT, content_encoding TEXT,
            content BLOB, last_collected_at TEXT
        );
        CREATE TABLE worker_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
        """
    )
    for number, ticker, body, filed in [
        (1, "NTCE", NOTICE, "2026-09-10T00:00:00+00:00"),
        (2, "OKAY", ROUTINE, "2026-09-11T00:00:00+00:00"),
        (3, "OLDN", NOTICE, "2026-05-01T00:00:00+00:00"),  # outside the 90 days
    ]:
        connection.execute(
            "INSERT INTO sec_filings VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                f"acc-{number}",
                ticker,
                "6-K",
                "",
                "New current report",
                "neutral",
                68.0,
                filed,
                _url(number),
                None,
            ),
        )
        connection.execute(
            "INSERT INTO source_documents VALUES ('sec',?,?,'identity',?,?)",
            (
                f"https://www.sec.gov/Archives/edgar/data/7/{number}/doc.htm",
                hashlib.sha256(body).hexdigest(),
                body,
                filed,
            ),
        )
    return connection


def _rows(database):
    return [tuple(row) for row in database.execute("SELECT accession,items,kind FROM sec_filings")]


def test_the_default_is_a_dry_run_that_changes_nothing(database):
    before = _rows(database)

    report = recheck_archived_6ks(database, at=AT)

    assert report["dry_run"] is True
    assert [n["ticker"] for n in report["would_mark"]] == ["NTCE"]
    assert report["six_ks"] == 2  # the 6-K from May is outside the window
    assert _rows(database) == before
    assert database.execute("SELECT COUNT(*) FROM worker_state").fetchone()[0] == 0


def test_apply_marks_the_notice_and_records_the_window_as_read(database):
    report = recheck_archived_6ks(database, at=AT, apply=True)

    assert report["tickers"] == ["NTCE"]
    assert report["window_marked_as_read"] is True
    marked = database.execute(
        "SELECT items,kind,sentiment FROM sec_filings WHERE accession='acc-1'"
    ).fetchone()
    assert tuple(marked) == ("listing-notice", "Exchange listing notice", "risk")
    assert (
        database.execute("SELECT items FROM sec_filings WHERE accession='acc-2'").fetchone()[0]
        == ""
    )
    assert foreign_notices_read(database, AT)


def test_a_6k_without_archived_text_keeps_the_window_unread(database):
    database.execute("DELETE FROM source_documents WHERE source_url LIKE '%/2/doc.htm'")

    report = recheck_archived_6ks(database, at=AT, apply=True)

    assert report["text_not_archived"] == 1
    assert report["window_marked_as_read"] is False
    assert not foreign_notices_read(database, AT)


def test_a_second_run_finds_nothing_new(database):
    recheck_archived_6ks(database, at=AT, apply=True)

    again = recheck_archived_6ks(database, at=AT)

    assert again["already_marked"] == 1 and again["would_mark"] == []
