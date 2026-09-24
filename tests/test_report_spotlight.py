import sqlite3

import pytest

from runner_web.report_spotlight import decorate_edition, freeze_spotlight, select_spotlight


def candidate(ticker="TEST", **changes):
    return {
        "ticker": ticker,
        "price": 4.0,
        "score": 20,
        "change_pct": 1,
        "relative_volume": 1,
        "quote_time": "2026-09-23T20:10:00+00:00",
        "signals": [],
        "risks": [],
        **changes,
    }


def test_selection_reads_both_directions_and_more_than_the_largest_gain():
    gain = candidate("GAIN", change_pct=40)
    story = candidate(
        "STORY",
        change_pct=-18,
        relative_volume=5,
        signals=["Volume acceleration", "Below prior low"],
        risks=["Thin liquidity", "Short cash runway"],
    )
    assert select_spotlight([gain, story]) == story
    assert select_spotlight([candidate("Z"), candidate("A")])["ticker"] == "A"


@pytest.mark.parametrize("price", [None, 0, -1, float("nan"), float("inf")])
def test_unpriced_names_are_skipped(price):
    assert select_spotlight([candidate(price=price)]) is None


def test_missing_volume_and_invalid_changes_stay_bounded():
    corrupt = candidate("Z", change_pct=float("inf"), relative_volume=float("nan"))
    assert select_spotlight([corrupt, candidate("A")])["ticker"] == "A"


def test_profile_uses_only_known_company_facts_and_filings():
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.executescript("""
        CREATE TABLE sec_companies(ticker, cik, name, exchange, sic_description,
                                  refreshed_at, sector_refreshed_at);
        CREATE TABLE sec_filings(ticker,form,title,filed_at,filing_url,created_at,accession);
        CREATE TABLE issuer_facts(id,cik,concept,value,unit,period_start,period_end,
                                 filed_at,accession,first_collected_at);
        INSERT INTO sec_companies VALUES('TEST',123,'Test Energy','NASDAQ','Energy storage',
                                        '2026-09-20','2026-09-20');
        INSERT INTO sec_filings VALUES('TEST','8-K','Saved event','2026-09-23',
                                       'https://www.sec.gov/example','2026-09-23','a');
        INSERT INTO sec_filings VALUES('TEST','8-K','Later event','2026-09-24',
                                       'https://www.sec.gov/later','2026-09-24','b');
        INSERT INTO sec_filings VALUES('TEST','10-Q','Late collected','2026-09-20',
                                       'https://www.sec.gov/late','2026-09-24','c');
        INSERT INTO issuer_facts VALUES('cash',123,'cash',1200000,'USD',NULL,'2026-06-30',
                                       '2026-08-10','0000000123-26-000001','2026-08-10');
        INSERT INTO issuer_facts VALUES('future',123,'cash',99000000,'USD',NULL,'2026-09-30',
                                       '2026-10-10','0000000123-26-000002','2026-10-10');
        INSERT INTO issuer_facts VALUES('late',123,'debt_total',123000000,'USD',NULL,
                    '2026-06-30','2026-08-10','0000000123-26-000001','2026-09-24');
    """)
    try:
        saved = freeze_spotlight(
            database,
            [candidate(), candidate("OLD", change_pct=100, quote_time="2026-09-22")],
            "2026-09-23T20:10:00+00:00",
            "2026-09-23T20:20:00+00:00",
        )
        assert saved["company"]["name"] == "Test Energy"
        assert saved["eligible"] == 1
        assert [row["title"] for row in saved["filings"]] == ["Saved event"]
        assert [row["value"] for row in saved["facts"]] == [1200000]
        assert saved["facts"][0]["source_url"].endswith("0000000123-26-000001-index.html")
    finally:
        database.close()


def test_legacy_edition_preserves_unknown_rings_and_closing_breadth():
    report = {
        "report_type": "post_market",
        "report_day": "2026-09-23",
        "leaders": [candidate()],
        "metrics": {"candidates": 200, "green": 180},
    }
    decorate_edition(report)
    assert report["spotlight"] is None
    assert report["edition"]["breadth"] == []
    assert report["edition"]["hero"]["indicator"]["mix_state"] == "unknown"
    report["metrics"]["closing_breadth"] = {"candidates": 10, "green": 3, "red": 5}
    decorate_edition(report)
    assert [part["count"] for part in report["edition"]["breadth"]] == [3, 5, 2]
    assert sum(part["percent"] for part in report["edition"]["breadth"]) == 100
