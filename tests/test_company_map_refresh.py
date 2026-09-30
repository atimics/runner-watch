from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from runner_watch.edgar import EdgarCompany
from runner_web import db, intelligence
from runner_web.db import connection, init_db

OLD = (datetime.now(UTC) - timedelta(days=2)).isoformat()


class FakeClient:
    def __init__(self, companies: list[EdgarCompany]) -> None:
        self._companies = companies

    def companies(self) -> list[EdgarCompany]:
        return self._companies


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "company-map.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    init_db()


def _seed(rows: list[tuple]) -> None:
    with connection() as database:
        for cik, ticker, sic, description, looked_up in rows:
            database.execute(
                "INSERT INTO sec_companies("
                "cik,ticker,name,exchange,refreshed_at,sic,sic_description,sector_refreshed_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (cik, ticker, ticker + " Inc", "Nasdaq", OLD, sic, description, looked_up),
            )


def _companies() -> dict[str, dict]:
    with connection() as database:
        rows = database.execute(
            "SELECT ticker,cik,exchange,sic,sic_description,sector_refreshed_at,refreshed_at "
            "FROM sec_companies"
        ).fetchall()
    return {row["ticker"]: dict(row) for row in rows}


def test_rebuilding_the_company_list_keeps_the_sic_codes_already_read(database) -> None:
    _seed(
        [
            (1, "BANK", "6022", "State Commercial Banks", OLD),
            (1, "BANKP", "6022", "State Commercial Banks", OLD),  # a second ticker, same company
            (2, "TRIED", None, None, OLD),  # looked up, the SEC had no code
            (3, "QUEUED", None, None, None),  # never looked up
            (4, "GONE", "3590", "Machinery", OLD),  # no longer in the SEC list
        ]
    )
    client = FakeClient(
        [
            EdgarCompany(cik=1, name="Bank", ticker="BANK", exchange="NYSE"),
            EdgarCompany(cik=1, name="Bank", ticker="BANKP", exchange="NYSE"),
            EdgarCompany(cik=2, name="Tried", ticker="TRIED", exchange="Nasdaq"),
            EdgarCompany(cik=3, name="Queued", ticker="QUEUED", exchange="Nasdaq"),
            EdgarCompany(cik=5, name="New", ticker="NEWCO", exchange="Nasdaq"),
        ]
    )

    assert intelligence.refresh_company_map(client) == 5

    companies = _companies()
    assert set(companies) == {"BANK", "BANKP", "TRIED", "QUEUED", "NEWCO"}
    for ticker in ("BANK", "BANKP"):
        assert companies[ticker]["sic"] == "6022"
        assert companies[ticker]["sic_description"] == "State Commercial Banks"
        assert companies[ticker]["sector_refreshed_at"] == OLD
        assert companies[ticker]["exchange"] == "NYSE"  # the list's own fields do refresh
        assert companies[ticker]["refreshed_at"] != OLD
    assert companies["TRIED"]["sic"] is None and companies["TRIED"]["sector_refreshed_at"] == OLD
    assert companies["QUEUED"]["sector_refreshed_at"] is None
    assert companies["NEWCO"]["sic"] is None and companies["NEWCO"]["sector_refreshed_at"] is None
