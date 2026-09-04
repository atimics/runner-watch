from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db, outcomes
from runner_web.cases import create_case, get_case
from runner_web.db import connection, init_db
from runner_web.outcomes import (
    barrier_outcome,
    case_horizon_outcome,
    due_horizons,
    return_pct,
)


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


def test_barrier_outcome_uses_highs_lows_and_first_touch() -> None:
    base = datetime(2026, 8, 24, 14, tzinfo=UTC)
    bars = [
        (base + timedelta(minutes=5), 103.0, 99.0, 102.0),
        (base + timedelta(minutes=10), 108.5, 101.0, 107.0),
        (base + timedelta(minutes=15), 109.0, 95.0, 96.0),
    ]
    result = barrier_outcome(bars, base, 100.0)
    assert result is not None
    assert result["barrier_label"] == "up"
    assert result["barrier_hit_at"] == bars[1][0].isoformat()
    assert result["max_favorable_pct"] == 9.0


def test_barrier_outcome_marks_same_bar_ambiguity_as_down() -> None:
    base = datetime(2026, 8, 24, 14, tzinfo=UTC)
    result = barrier_outcome([(base + timedelta(minutes=5), 109.0, 95.0, 101.0)], base, 100.0)
    assert result is not None
    assert result["barrier_label"] == "down"
    assert result["barrier_ambiguous"] == 1


def test_timeout_requires_bars_covering_the_full_hour() -> None:
    base = datetime(2026, 8, 24, 14, tzinfo=UTC)
    complete = [
        (base + timedelta(minutes=minute), 102.0, 98.0, 100.0) for minute in range(5, 61, 5)
    ]
    result = barrier_outcome(complete, base, 100.0)
    assert result is not None
    assert result["barrier_label"] == "timeout"
    assert barrier_outcome(complete[:4], base, 100.0) is None


def test_barrier_outcome_rejects_an_internal_bar_gap() -> None:
    base = datetime(2026, 8, 24, 14, tzinfo=UTC)
    bars = [
        (base + timedelta(minutes=5), 102.0, 98.0, 100.0),
        (base + timedelta(minutes=55), 109.0, 98.0, 108.0),
    ]

    assert barrier_outcome(bars, base, 100.0) is None


def test_case_horizon_uses_the_first_archived_bar_after_its_due_time() -> None:
    base = datetime(2026, 8, 24, 14, tzinfo=UTC)
    bars = [
        (base + timedelta(minutes=30), 101.0, 98.0, 100.0),
        (base + timedelta(minutes=65), 106.0, 99.0, 105.0),
        (base + timedelta(minutes=70), 120.0, 80.0, 90.0),
    ]

    result = case_horizon_outcome(
        bars,
        base,
        100.0,
        60,
        at=base + timedelta(minutes=90),
    )

    assert result is not None
    assert result["end_price"] == 105.0
    assert result["return_pct"] == 5.0
    assert result["max_favorable_pct"] == 6.0
    assert result["max_adverse_pct"] == -2.0


def test_case_outcome_closes_the_view_at_its_inferred_horizon(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    current = datetime.now(UTC) + timedelta(minutes=70)
    base_at = current - timedelta(minutes=70)
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "case-horizon.db")
    monkeypatch.setattr(outcomes, "_latest_prices", lambda tickers: {"PEN": 2.2})
    init_db()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("case-owner", "case_owner", "Case Owner", "active", base_at.isoformat()),
        )
    case = create_case(
        "case-owner",
        "PEN",
        thesis="Watching PEN for one hour.",
        horizon_minutes=60,
        reference_price=2.0,
        invalidation="Unknown — not supplied by the user.",
        risks=[],
        open_questions=[],
        confidence=None,
    )
    observed_at = base_at + timedelta(minutes=65)
    with connection() as database:
        database.execute(
            "UPDATE thesis_cases SET reference_at=? WHERE id=?",
            (base_at.isoformat(), case["id"]),
        )
        database.execute(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,high,low,close,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "yahoo",
                "PEN",
                "5m",
                observed_at.isoformat(),
                2.3,
                1.9,
                2.2,
                current.isoformat(),
                current.isoformat(),
            ),
        )

    result = outcomes.refresh_case_outcomes(current)
    resolved = get_case("case-owner", case["public_id"])

    assert result["completed"] == 1
    assert resolved is not None
    assert resolved["status"] == "closed"
    assert resolved["outcome_status"] == "complete"
    assert resolved["outcome_return_pct"] == 10.0
    assert resolved["latest_summary"] == "1h view ended +10.0% at $2.2."


def test_refresh_outcomes_uses_archived_prices_at_each_due_horizon(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    current = datetime(2026, 8, 26, 20, tzinfo=UTC)
    base_at = current - timedelta(days=2, hours=6)
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "outcomes.db")
    monkeypatch.setattr(outcomes, "_latest_prices", lambda tickers: {"PEN": 3.0})
    init_db()
    created_at = base_at.isoformat()
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
        for stamp, price in (
            (base_at + timedelta(hours=1, minutes=5), 2.5),
            (base_at + timedelta(days=1, hours=6), 3.0),
        ):
            database.execute(
                """
                INSERT INTO market_bars(
                    source,ticker,interval,bar_time,close,
                    first_collected_at,last_collected_at
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

    result = outcomes.refresh_outcomes(current)
    with connection() as database:
        row = database.execute("SELECT * FROM sec_outcomes").fetchone()
    assert result["samples_added"] == 2
    assert row["return_1h_pct"] == 25.0
    assert row["return_1d_pct"] == 50.0
    assert row["return_5d_pct"] is None
    assert row["observed_1h_at"] != row["observed_1d_at"]


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
