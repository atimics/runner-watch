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
