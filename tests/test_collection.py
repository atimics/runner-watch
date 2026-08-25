from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from pytest import MonkeyPatch

from runner_watch.ingestion import SourceFetch
from runner_web import db
from runner_web.collection import record_market_bars, record_source_document
from runner_web.db import connection, init_db
from runner_web.ingestion import (
    ingestion_status,
    mark_source_item,
    record_source_fetch,
    source_item_is_terminal,
)


def test_collectors_keep_market_bars_and_distinct_source_versions(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "collection.db")
    init_db()
    index = pd.date_range("2026-08-24 09:30", periods=2, freq="5min", tz="America/New_York")
    frame = pd.DataFrame(
        {
            "Open": [1.0, 1.1],
            "High": [1.2, 1.3],
            "Low": [0.9, 1.0],
            "Close": [1.1, 1.2],
            "Volume": [1000, 1500],
        },
        index=index,
    )
    record_market_bars("5m", {"PEN": frame})
    revised = frame.copy()
    revised.loc[index[-1], "Close"] = 1.25
    record_market_bars("5m", {"PEN": revised})

    record_source_document("https://www.sec.gov/example.xml", b"<one />")
    record_source_document("https://www.sec.gov/example.xml", b"<one />")
    record_source_document("https://www.sec.gov/example.xml", b"<two />")

    with connection() as database:
        bars = database.execute(
            "SELECT COUNT(*),MAX(close) FROM market_bars WHERE ticker='PEN'"
        ).fetchone()
        documents = database.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
        runs = database.execute(
            "SELECT source,COUNT(*) AS count FROM ingestion_runs GROUP BY source"
        ).fetchall()
    assert bars[0] == 2
    assert bars[1] == 1.25
    assert documents == 2
    assert {row["source"]: row["count"] for row in runs} == {"sec": 3, "yahoo": 2}


def test_shared_pipe_records_universe_items_errors_and_terminal_source_items(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ingestion.db")
    init_db()
    started_at = datetime(2026, 8, 24, 20, tzinfo=UTC)
    record_source_fetch(
        SourceFetch.success(
            source="yahoo",
            feed="universe",
            locator="yfinance://screen/percentchange",
            started_at=started_at,
            payload=[
                {"symbol": "PEN", "exchange": "NCM", "intradayprice": 2.5},
                {"exchange": "NCM", "intradayprice": 1.5},
            ],
            content_type="application/json",
            metadata={"size": 175},
        )
    )
    record_source_fetch(
        SourceFetch.failure(
            source="sec",
            feed="current_filings",
            locator="https://www.sec.gov/current.atom",
            started_at=started_at,
            error="timed out",
        )
    )
    mark_source_item(
        source="sec",
        feed="filing",
        item_key="0001-26-000001",
        status="ignored",
        payload={"form": "N-PX"},
        error="Form is outside the configured filing set",
        parser_version="2",
    )

    with connection() as database:
        statuses = database.execute(
            "SELECT source,feed,status FROM ingestion_runs ORDER BY source"
        ).fetchall()
        items = database.execute(
            "SELECT item_key,status FROM ingestion_items ORDER BY item_key"
        ).fetchall()
    assert [tuple(row) for row in statuses] == [
        ("sec", "current_filings", "error"),
        ("yahoo", "universe", "success"),
    ]
    assert [tuple(row) for row in items] == [("PEN", "accepted"), ("ROW-1", "rejected")]
    assert source_item_is_terminal("sec", "filing", "0001-26-000001") is True
    status = ingestion_status()
    assert {feed["source"] for feed in status["feeds"]} == {"sec", "yahoo"}
    assert status["item_states"][0]["status"] == "ignored"
