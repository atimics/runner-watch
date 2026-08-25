from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.main import _pulse_label, pulse_data, ticker_detail_data


def insert_filing(
    accession: str,
    ticker: str,
    price: float,
    score: float,
    filed_at: str,
    transaction_codes: str = "",
) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                filing_url,transaction_codes,price,change_pct,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                accession,
                1,
                ticker,
                f"{ticker} Company",
                "4",
                "Insider open-market buy",
                "positive",
                score,
                f"4 - {ticker} Company",
                filed_at,
                f"https://www.sec.gov/{accession}",
                transaction_codes,
                price,
                6.5,
                filed_at,
                filed_at,
            ),
        )


def test_pulse_only_lists_penny_stocks_and_groups_events(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "mobile.db")
    init_db()
    filed_at = datetime.now(UTC).isoformat()
    insert_filing("penny-one", "PEN", 2.25, 80, filed_at, "P")
    insert_filing("penny-two", "PEN", 2.25, 70, filed_at, "P")
    insert_filing("big-one", "BIG", 42.0, 99, filed_at, "P")
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "big-snapshot",
                "HUGE",
                100,
                "BUILDING",
                "regular",
                60.0,
                12.0,
                2.0,
                5.0,
                4.0,
                5.0,
                1.0,
                2_000_000,
                filed_at,
                "[]",
                "[]",
                filed_at,
            ),
        )

    result = pulse_data()

    assert [row["ticker"] for row in result["rows"]] == ["PEN"]
    assert result["rows"][0]["event_count"] == 2
    assert result["stats"]["filings"] == 2


def test_ticker_detail_explains_form_four_purchase(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "detail.db")
    init_db()
    filed_at = datetime.now(UTC).isoformat()
    insert_filing("detail-one", "PEN", 1.75, 77, filed_at, "P")

    detail = ticker_detail_data("PEN")

    assert detail is not None
    assert detail["events"][0]["evidence_label"] == "Verified insider purchase"
    assert detail["events"][0]["pulse_label"] == "Form 4 · insider buy"


def test_pulse_label_does_not_call_a_sale_a_buy() -> None:
    assert _pulse_label({"transaction_codes": "S", "actor_title": "CEO"}) == (
        "Form 4 · insider sale"
    )
