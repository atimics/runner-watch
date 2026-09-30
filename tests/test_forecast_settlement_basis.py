"""A forecast says which price it is scored on; a foreign issuer's says more (#469)."""

from __future__ import annotations

import pytest

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.market_forecasts import generate_market_forecasts, settle_market_forecasts
from runner_web.market_reports import market_report
from tests.test_market_forecasts import CLOSE, DAY, PRE, _bar, _generate, _leader, _report


@pytest.fixture(autouse=True)
def forecast_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "settlement-basis.db")
    init_db()


def _file_forms(ticker: str, *forms: str) -> None:
    with connection() as database:
        for number, form in enumerate(forms):
            database.execute(
                "INSERT INTO sec_filings(accession,cik,ticker,company,form,kind,sentiment,score,"
                "title,filed_at,filing_url,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'k','neutral',1,'t','2026-08-01T00:00:00+00:00','u','x','x')",
                (f"{ticker}-{number}", 1, ticker, ticker, form),
            )


def test_a_foreign_issuers_forecast_says_it_is_scored_on_the_us_session():
    _file_forms("DOWN", "20-F", "6-K")
    _file_forms("UP", "10-Q", "8-K")
    _report()
    generate_market_forecasts(_generate, PRE)

    forecasts = {
        row["ticker"]: row["eod_forecast"] for row in market_report(DAY, "pre_market")["leaders"]
    }

    assert forecasts["DOWN"]["settlement_note"].startswith("Scored on the US session only")
    assert forecasts["DOWN"]["settlement_basis"] == "US regular session close, 4 p.m. ET"
    # A domestic issuer carries the basis but no extra note.
    assert forecasts["UP"]["settlement_note"] is None
    assert forecasts["UP"]["status"] == "pending"


def test_the_note_does_not_change_how_a_forecast_settles():
    _file_forms("UP", "20-F")
    _report(leaders=[_leader("UP")])
    generate_market_forecasts(_generate, PRE)
    _bar("UP", 1.2)

    assert settle_market_forecasts(CLOSE, fetch_market_data=False)["resolved"] == 1
    forecast = market_report(DAY, "pre_market")["leaders"][0]["eod_forecast"]

    assert forecast["status"] == "hit" and forecast["close_price"] == 1.2
