import gzip
import sqlite3

import pytest

from runner_web.report_company import business_excerpt, freeze_company_context, price_chart
from runner_web.report_spotlight import timely_quote


@pytest.mark.parametrize(
    ("quote", "eligible"),
    [
        ("2026-09-23T19:50:00+00:00", True),
        ("2026-09-23T19:49:59+00:00", False),
        ("2026-09-23T20:10:01+00:00", False),
        ("2026-09-23T13:35:00+00:00", False),
        ("2026-09-22T20:10:00+00:00", False),
        ("2026-09-23T16:08:00-04:00", True),
        (None, False),
        ("garbled", False),
    ],
)
def test_editorial_quotes_are_bounded_by_age_and_saved_scan(quote, eligible):
    assert timely_quote({"quote_time": quote}, "2026-09-23T20:10:00+00:00") is eligible


BUSINESS = (
    "We build battery systems for industrial sites. Our customers use these systems "
    "to store power and manage demand during busy periods. The company sells hardware "
    "and provides ongoing maintenance for installed systems."
)


def test_business_excerpt_skips_contents_and_keeps_the_source_words():
    html = (
        "<p>Item 1. Business .... 4</p><p>Item 1A Risk factors .... 12</p>"
        "<h2>Item 1. <b>Business</b></h2><script>invented text</script>"
        f"<p>{BUSINESS}</p><h2>Item 1A Risk factors</h2><p>Later content.</p>"
    ).encode()
    assert business_excerpt(html, "identity") == BUSINESS
    assert business_excerpt(gzip.compress(html), "gzip") == BUSINESS
    assert business_excerpt(b"<p>Item 1. Business .... 4</p>", "identity") is None
    assert business_excerpt(b"broken gzip", "gzip") is None
    assert business_excerpt(gzip.compress(b"x" * 2_000_001), "gzip") is None


def test_profile_freezes_archived_business_and_timed_prices():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE sec_filings(ticker,form,title,filed_at,filing_url,created_at,accession);
        CREATE TABLE source_documents(source,source_url,content,content_encoding,content_hash,
                                      first_collected_at);
        CREATE TABLE scan_snapshots(id,ticker,price,quote_time,captured_at);
    """)
    url = "https://www.sec.gov/Archives/edgar/data/1/0001/annual.htm"
    db.execute(
        "INSERT INTO sec_filings VALUES(?,?,?,?,?,?,?)",
        ("TEST", "10-K", "Annual report", "2026-03-01", url, "2026-03-02", "a"),
    )
    for stamp, paragraph in [("2026-03-02", BUSINESS), ("2026-09-24", "Later " + BUSINESS)]:
        db.execute(
            "INSERT INTO source_documents VALUES(?,?,?,?,?,?)",
            (
                "sec",
                url,
                f"<h2>Item 1. Business</h2><p>{paragraph}</p>".encode(),
                "identity",
                stamp,
                stamp,
            ),
        )
    db.executemany(
        "INSERT INTO scan_snapshots VALUES(?,?,?,?,?)",
        [
            ("first", "TEST", 4, "2026-09-23T14:00:00+00:00", "2026-09-23T14:01:00+00:00"),
            ("stale", "TEST", 99, "2026-09-23T14:00:00+00:00", "2026-09-23T15:00:00+00:00"),
            ("future", "TEST", 88, "2026-09-23T20:11:00+00:00", "2026-09-23T20:09:00+00:00"),
            ("last", "TEST", 5, "2026-09-23T20:08:00+00:00", "2026-09-23T20:10:00+00:00"),
            ("later", "TEST", 7, "2026-09-23T20:20:00+00:00", "2026-09-23T20:21:00+00:00"),
        ],
    )
    feature = {
        "ticker": "TEST",
        "as_of": "2026-09-23T20:10:00+00:00",
        "captured_at": "2026-09-23T20:20:00+00:00",
        "filings": [
            {"title": "Session event", "filed_at": "2026-09-23"},
            {"title": "Earlier event", "filed_at": "2026-09-22"},
        ],
    }
    try:
        freeze_company_context(db, feature)
        assert feature["business"]["text"] == BUSINESS
        assert feature["business"]["source_url"] == url
        assert [p["price"] for p in feature["price_trace"]] == [4, 5]
        assert [f["title"] for f in feature["session_events"]] == ["Session event"]
        chart = price_chart(feature["price_trace"])
        assert chart["low"] == 4 and chart["high"] == 5
        assert chart["points"] == "8.0,100.0 592.0,20.0"
        # All display inputs survive independently of later source edits.
        db.execute("UPDATE source_documents SET content=?", (b"changed",))
        db.execute("UPDATE scan_snapshots SET price=777")
        assert feature["business"]["text"] == BUSINESS
        assert feature["price_trace"][-1]["price"] == 5
    finally:
        db.close()


def test_chart_handles_flat_or_single_observations():
    assert price_chart([]) is None
    rows = [
        {"price": 4, "quote_time": "2026-09-23T14:00:00+00:00"},
        {"price": 4, "quote_time": "2026-09-23T15:00:00+00:00"},
    ]
    assert price_chart(rows)["points"] == "8.0,60.0 592.0,60.0"
    assert price_chart(rows[:1]) is None
