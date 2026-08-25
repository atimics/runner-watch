from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db, outcomes
from runner_web.db import connection, init_db
from runner_web.outcomes import due_horizons, return_pct


def outcome_row(base_at: datetime) -> dict[str, object]:
    return {
        "base_at": base_at.isoformat(),
        "return_1h_pct": None,
        "return_1d_pct": None,
        "return_5d_pct": None,
    }


def test_return_pct_uses_the_original_observed_price() -> None:
    assert return_pct(2.0, 2.5) == 25.0
    assert return_pct(2.0, 1.5) == -25.0
    assert return_pct(0, 2.5) is None


def test_due_horizons_only_returns_mature_missing_samples() -> None:
    current = datetime(2026, 8, 24, 20, tzinfo=UTC)
    row = outcome_row(current - timedelta(days=2))
    row["return_1h_pct"] = 4.2
    assert due_horizons(row, current) == ["1d"]


def test_five_day_outcome_waits_for_five_days() -> None:
    current = datetime(2026, 8, 24, 20, tzinfo=UTC)
    assert due_horizons(outcome_row(current - timedelta(days=6)), current) == [
        "1h",
        "1d",
        "5d",
    ]


def test_refresh_outcomes_labels_only_due_horizons(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    current = datetime(2026, 8, 24, 20, tzinfo=UTC)
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "outcomes.db")
    monkeypatch.setattr(outcomes, "_latest_prices", lambda tickers: {"PEN": 3.0})
    init_db()
    created_at = (current - timedelta(days=2)).isoformat()
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                filing_url,transaction_codes,price,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "0001-26-000001",
                1,
                "PEN",
                "Penny Inc.",
                "8-K",
                "New current report",
                "neutral",
                60,
                "8-K - Penny Inc.",
                created_at,
                "https://www.sec.gov/example",
                "",
                2.0,
                created_at,
                created_at,
            ),
        )

    result = outcomes.refresh_outcomes(current)
    with connection() as database:
        row = database.execute("SELECT * FROM sec_outcomes").fetchone()
    assert result["samples_added"] == 2
    assert row["return_1h_pct"] == 50.0
    assert row["return_1d_pct"] == 50.0
    assert row["return_5d_pct"] is None


def test_scan_outcomes_use_first_archived_bar_after_horizon(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    current = datetime(2026, 8, 24, 20, tzinfo=UTC)
    base_at = current - timedelta(days=2)
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "scan-outcomes.db")
    monkeypatch.setattr(outcomes, "_latest_prices", lambda tickers: {"PEN": 9.0})
    init_db()
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_runs(
                id,mode,label,feature_schema_version,requested_symbols,liquid_symbols,
                scanned_symbols,candidate_rows,failed_symbols_json,warnings_json,
                started_at,finished_at,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "run-1",
                "penny",
                "Penny stocks",
                "stonks.ranker_features.v1",
                1,
                1,
                1,
                1,
                "[]",
                "[]",
                base_at.isoformat(),
                base_at.isoformat(),
                base_at.isoformat(),
            ),
        )
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,breakout_pct,dollar_volume,quote_time,signals_json,
                risks_json,captured_at,scan_run_id,baseline_rank,range_position,stale_minutes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "snapshot-1",
                "PEN",
                50.0,
                "WATCH",
                "REGULAR",
                2.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1_000_000.0,
                base_at.isoformat(),
                "[]",
                "[]",
                base_at.isoformat(),
                "run-1",
                1,
                0.5,
                0.0,
            ),
        )
        for stamp, price in (
            (base_at + timedelta(hours=1, minutes=5), 2.5),
            (base_at + timedelta(days=1, minutes=5), 3.0),
        ):
            database.execute(
                """
                INSERT INTO market_bars(
                    source,ticker,interval,bar_time,close,first_collected_at,last_collected_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    "yahoo",
                    "PEN",
                    "5m",
                    stamp.isoformat(),
                    price,
                    current.isoformat(),
                    current.isoformat(),
                ),
            )

    result = outcomes.refresh_scan_outcomes(current)
    with connection() as database:
        row = database.execute("SELECT * FROM scan_outcomes").fetchone()
    assert result["samples_added"] == 2
    assert row["return_1h_pct"] == 25.0
    assert row["return_1d_pct"] == 50.0
    assert row["return_5d_pct"] is None
