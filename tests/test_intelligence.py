from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_watch.edgar import EdgarFiling
from runner_web import db, intelligence
from runner_web.db import connection, init_db


class MissingCompanyEdgarClient:
    def __init__(self) -> None:
        self.ownership_calls = 0
        self.filing = EdgarFiling(
            accession="0001-26-000001",
            cik=999,
            form="4",
            title="4 - Missing Company",
            role="Issuer",
            filed_at="2026-08-24T18:00:00-04:00",
            filing_url=("https://www.sec.gov/Archives/edgar/data/999/0001/0001-index.htm"),
        )

    def latest_filings(self) -> list[EdgarFiling]:
        return [self.filing]

    def ownership_summary(self, filing: EdgarFiling) -> None:
        self.ownership_calls += 1
        return None


def test_ignored_sec_item_is_not_reprocessed_on_every_poll(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "intelligence.db")
    init_db()
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at)
            VALUES(?,?,?,?,?)
            """,
            (1, "REAL", "Real Company", "Nasdaq", datetime.now(UTC).isoformat()),
        )
    client = MissingCompanyEdgarClient()
    monkeypatch.setattr(intelligence, "EdgarClient", lambda **kwargs: client)

    intelligence.refresh_edgar()
    intelligence.refresh_edgar()

    with connection() as database:
        state = database.execute(
            """
            SELECT status,attempt_count,error FROM source_item_state
            WHERE source='sec' AND feed='filing' AND item_key=?
            """,
            (client.filing.accession,),
        ).fetchone()
    assert client.ownership_calls == 1
    assert state["status"] == "ignored"
    assert state["attempt_count"] == 1
    assert state["error"] == "Issuer is not in the listed-company map"


class ForeignNoticeEdgarClient:
    """A foreign issuer's 6-K whose text reports a Nasdaq deficiency notice."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.filing = EdgarFiling(
            accession="0002-26-000009",
            cik=7,
            form="6-K",
            title="6-K - Foreign Co",
            role="Filer",
            filed_at="2026-09-10T16:05:00-04:00",
            filing_url="https://www.sec.gov/Archives/edgar/data/7/0002/0002-index.htm",
        )

    def latest_filings(self) -> list[EdgarFiling]:
        return [self.filing]

    def primary_filing_text(self, filing: EdgarFiling) -> tuple[str, str]:
        return f"{filing.filing_url}/doc.htm", self.text


def _ingest_6k(tmp_path: Path, monkeypatch: MonkeyPatch, text: str) -> dict:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "foreign.db")
    init_db()
    with connection() as database:
        database.execute(
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) VALUES(?,?,?,?,?)",
            (7, "FRGN", "Foreign Co", "Nasdaq", datetime.now(UTC).isoformat()),
        )
    monkeypatch.setattr(
        intelligence, "EdgarClient", lambda **kwargs: ForeignNoticeEdgarClient(text)
    )
    monkeypatch.setattr(intelligence, "_market_context", lambda tickers: {})
    monkeypatch.setattr(intelligence, "_companyfacts_candidate", lambda events: None)

    intelligence.refresh_edgar()

    with connection() as database:
        return dict(
            database.execute(
                "SELECT form,items,kind,sentiment FROM sec_filings WHERE ticker='FRGN'"
            ).fetchone()
        )


def test_a_6k_deficiency_notice_is_marked_for_the_trading_standard(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    row = _ingest_6k(
        tmp_path,
        monkeypatch,
        "<p>The Company received a notification letter from Nasdaq stating it is not in "
        "compliance with Nasdaq Listing Rule 5550(a)(2).</p>",
    )

    assert row == {
        "form": "6-K",
        "items": "listing-notice",
        "kind": "Exchange listing notice",
        "sentiment": "risk",
    }


def test_an_ordinary_6k_is_left_as_a_current_report(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    row = _ingest_6k(tmp_path, monkeypatch, "<p>Interim results for the six months.</p>")

    assert row["items"] in ("", None) and row["kind"] == "New current report"
