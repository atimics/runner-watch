"""The one-off re-read of archived 6-Ks (#469): a dry run unless told to apply."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

import pytest

from runner_web import sec_6k_recheck
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


class FakeFetcher:
    """Stands in for SEC EDGAR: no network is ever used."""

    def __init__(self, texts: dict[str, str | Exception | None]):
        self.texts = texts
        self.calls: list[str] = []

    def __call__(self, filing_url: str, accession: str) -> str | None:
        self.calls.append(accession)
        result = self.texts.get(accession)
        if isinstance(result, Exception):
            raise result
        return result


def _without_archive(database):
    database.execute("DELETE FROM source_documents")


def test_without_fetch_nothing_is_fetched_even_when_text_is_missing(database):
    _without_archive(database)

    report = recheck_archived_6ks(database, at=AT)

    assert report["text_not_archived"] == 2
    assert report["fetched"] == 0 and report["fetch_failed"] == 0


def test_fetch_reads_missing_texts_and_finds_the_notice_without_writing(database):
    _without_archive(database)
    before = _rows(database)
    fetcher = FakeFetcher({"acc-1": NOTICE.decode(), "acc-2": ROUTINE.decode()})

    report = recheck_archived_6ks(database, at=AT, fetch_text=fetcher)

    assert fetcher.calls == ["acc-1", "acc-2"]  # the old 6-K outside the window is skipped
    assert [n["ticker"] for n in report["would_mark"]] == ["NTCE"]
    assert report["fetched"] == 2 and report["text_not_archived"] == 0
    assert _rows(database) == before
    assert database.execute("SELECT COUNT(*) FROM worker_state").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 0


def test_archived_text_is_not_fetched_again(database):
    fetcher = FakeFetcher({})

    recheck_archived_6ks(database, at=AT, fetch_text=fetcher)

    assert fetcher.calls == []


def test_max_fetch_bounds_the_downloads_and_leaves_the_rest_unread(database):
    _without_archive(database)
    fetcher = FakeFetcher({"acc-1": NOTICE.decode(), "acc-2": ROUTINE.decode()})

    report = recheck_archived_6ks(database, at=AT, apply=True, fetch_text=fetcher, max_fetch=1)

    assert fetcher.calls == ["acc-1"]
    assert report["fetched"] == 1 and report["text_not_archived"] == 1
    assert report["window_marked_as_read"] is False
    assert not foreign_notices_read(database, AT)


def test_max_fetch_zero_fetches_nothing(database):
    _without_archive(database)
    fetcher = FakeFetcher({"acc-1": NOTICE.decode()})

    report = recheck_archived_6ks(database, at=AT, fetch_text=fetcher, max_fetch=0)

    assert fetcher.calls == [] and report["text_not_archived"] == 2


def test_a_failed_fetch_keeps_the_window_unread_and_does_not_stop_the_run(database):
    _without_archive(database)
    fetcher = FakeFetcher({"acc-1": OSError("network down"), "acc-2": ROUTINE.decode()})

    report = recheck_archived_6ks(database, at=AT, apply=True, fetch_text=fetcher)

    assert fetcher.calls == ["acc-1", "acc-2"]
    assert report["fetch_failed"] == 1 and report["text_not_archived"] == 1
    assert report["window_marked_as_read"] is False
    assert not foreign_notices_read(database, AT)


def test_fetch_with_apply_marks_the_notice_and_reads_the_window(database):
    _without_archive(database)
    fetcher = FakeFetcher({"acc-1": NOTICE.decode(), "acc-2": ROUTINE.decode()})

    report = recheck_archived_6ks(database, at=AT, apply=True, fetch_text=fetcher)

    assert report["tickers"] == ["NTCE"] and report["window_marked_as_read"] is True
    assert foreign_notices_read(database, AT)


def test_the_real_fetcher_is_slow_polite_and_archives_only_with_apply(monkeypatch):
    made: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs):
            made.append(kwargs)

        def primary_filing_text(self, filing):
            return ("https://www.sec.gov/x", f"text of {filing.accession}")

    monkeypatch.setattr(sec_6k_recheck, "EdgarClient", FakeClient)

    dry = sec_6k_recheck.edgar_text_fetcher(archive=False)
    apply = sec_6k_recheck.edgar_text_fetcher(archive=True)

    assert dry(_url(1), "acc-1") == "text of acc-1"
    assert made[0]["fetch_recorder"] is None
    assert made[1]["fetch_recorder"] is not None
    for kwargs in made:
        assert kwargs["max_requests_per_second"] <= 2.0  # SEC allows 10
        assert kwargs["user_agent"] == sec_6k_recheck.SEC_USER_AGENT
    assert apply is not dry


def test_the_command_line_fetches_only_when_asked_and_bounds_it(monkeypatch):
    import contextlib

    from runner_web import db

    calls: list[dict] = []
    archive_flags: list[bool] = []

    def fake_recheck(database, **kwargs):
        calls.append(kwargs)
        return {}

    def fake_fetcher(*, archive):
        archive_flags.append(archive)
        return "FETCHER"

    @contextlib.contextmanager
    def fake_connection():
        yield object()

    monkeypatch.setattr(sec_6k_recheck, "recheck_archived_6ks", fake_recheck)
    monkeypatch.setattr(sec_6k_recheck, "edgar_text_fetcher", fake_fetcher)
    monkeypatch.setattr(db, "connection", fake_connection)

    def run(*args):
        monkeypatch.setattr("sys.argv", ["sec_6k_recheck", *args])
        sec_6k_recheck.main()

    run()
    run("--fetch")
    run("--fetch", "--apply", "--max-fetch", "7")

    assert calls[0]["fetch_text"] is None and calls[0]["max_fetch"] == 50
    assert calls[1]["fetch_text"] == "FETCHER" and calls[1]["apply"] is False
    assert calls[2]["apply"] is True and calls[2]["max_fetch"] == 7
    assert archive_flags == [False, True]  # a fetch alone archives nothing
    with pytest.raises(SystemExit):
        run("--max-fetch", "-1")
