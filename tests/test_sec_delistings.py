from __future__ import annotations

import json
from datetime import UTC, datetime

from runner_web import db, sec_delistings

AT = datetime(2026, 9, 28, 12, tzinfo=UTC)


def hit(adsh, items, name="LEGGETT & PLATT INC  (LEG)  (CIK 0000058492)", cik="0000058492"):
    return {
        "_source": {
            "adsh": adsh,
            "items": items,
            "ciks": [cik],
            "display_names": [name],
            "form": "8-K",
            "file_date": "2026-08-26",
        }
    }


def pages(*batches, amended=()):
    """8-K pages in order, then one 8-K/A page."""

    total = sum(len(batch) for batch in batches)
    calls = []

    def download(url, timeout):
        calls.append(url)
        if "8-K%2FA" in url:
            batch, count = list(amended), len(amended)
        else:
            batch, count = batches[len(calls) - 1], total
        return json.dumps({"hits": {"total": {"value": count}, "hits": batch}}).encode()

    return download, calls


def test_only_filings_whose_items_include_3_01_are_notices():
    download, calls = pages(
        [hit("0001-26-1", ["3.01", "9.01"]), hit("0001-26-2", ["8.01"])],
        [hit("0001-26-3", ["2.01", "3.01"], name="Talkspace, Inc.  (TALK)  (CIK 0001803901)")],
        amended=[hit("0001-26-4", ["3.01"])],
    )

    notices = sec_delistings.search_notices(at=AT, download=download)

    assert [(n["accession"], n["ticker"], n["items"]) for n in notices] == [
        ("0001-26-1", "LEG", "3.01,9.01"),
        ("0001-26-3", "TALK", "2.01,3.01"),
        ("0001-26-4", "LEG", "3.01"),
    ]
    assert len(calls) == 3 and "startdt=2026-06-30" in calls[0] and "from=2" in calls[1]
    assert "forms=8-K%2FA" in calls[2]


def test_notices_are_stored_and_fill_in_items_on_filings_already_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "d.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    with db.connection() as database:
        database.execute(
            "INSERT INTO sec_filings(accession,cik,ticker,company,form,kind,sentiment,score,"
            "title,filed_at,filing_url,created_at,updated_at,items) "
            "VALUES('0001-26-1',58492,'LEG','Leggett','8-K','k','neutral',1,'t',"
            "'2026-08-26T00:00:00+00:00','u','x','x','')"
        )
    download, _ = pages(
        [hit("0001-26-1", ["3.01"]), hit("0001-26-9", ["3.01"], name="No Ticker Co (CIK 1)")]
    )

    state = sec_delistings.refresh_delisting_notices(at=AT, download=download)

    with db.connection() as database:
        rows = database.execute("SELECT accession,items FROM sec_filings").fetchall()
        assert [tuple(row) for row in rows] == [("0001-26-1", "3.01")]
        assert sec_delistings.notices_read(database, AT)
        assert not sec_delistings.notices_read(database, AT.replace(day=30))
    assert state["notices"] == 2 and state["stored"] == 1


def test_a_search_that_fails_once_is_retried(monkeypatch):
    import urllib.error

    monkeypatch.setattr(sec_delistings, "RETRY_PAUSE", 0)
    download, calls = pages([hit("0001-26-1", ["3.01"])])
    failed = []

    def flaky(url, timeout):
        if not failed:
            failed.append(url)
            raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, None)
        return download(url, timeout)

    assert [n["accession"] for n in sec_delistings.search_notices(at=AT, download=flaky)] == [
        "0001-26-1"
    ]
