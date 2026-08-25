from pathlib import Path

import pandas as pd
from pytest import MonkeyPatch

from runner_web import db
from runner_web.collection import record_market_bars, record_source_document
from runner_web.db import connection, init_db


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
    assert bars[0] == 2
    assert bars[1] == 1.25
    assert documents == 2
