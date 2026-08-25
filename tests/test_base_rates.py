from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.base_rates import empirical_receipt, matched_market_base_rates
from runner_web.db import connection, init_db


def test_empirical_receipt_abstains_when_history_is_thin() -> None:
    receipt = empirical_receipt(8, [1, 2, 3], label="Mentions")

    assert receipt["mode"] == "insufficient_data"
    assert receipt["expected"] == 2
    assert receipt["percentile"] is None
    assert "need 20" in receipt["insufficient_reason"]


def test_market_base_rate_matches_one_same_clock_snapshot_per_day(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "base-rates.db")
    init_db()
    observed_at = datetime(2026, 8, 25, 15, tzinfo=UTC)
    with connection() as database:
        for day in range(1, 21):
            captured_at = observed_at - timedelta(days=day)
            database.execute(
                """
                INSERT INTO scan_snapshots(
                    id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                    momentum_15m_pct,momentum_acceleration_pct,relative_volume,
                    recent_relative_volume,vwap_position_pct,breakout_pct,dollar_volume,
                    quote_time,signals_json,risks_json,captured_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"matched-{day}",
                    "MATCH",
                    20,
                    "EARLY",
                    "regular",
                    1.0,
                    1.0,
                    0.5,
                    1.0,
                    0.1,
                    1.0 + day / 100,
                    1.2 + day / 100,
                    0.1,
                    0.0,
                    100_000,
                    captured_at.isoformat(),
                    "[]",
                    "[]",
                    captured_at.isoformat(),
                ),
            )

    result = matched_market_base_rates(
        {
            "ticker": "MATCH",
            "session": "regular",
            "captured_at": observed_at.isoformat(),
            "relative_volume": 4.0,
            "recent_relative_volume": 5.0,
            "momentum_15m_pct": 4.0,
        }
    )

    assert result["mode"] == "empirical"
    assert result["method_version"] == 1
    assert result["as_of"] == observed_at.isoformat()
    assert result["matched_sessions"] == 20
    assert result["metrics"]["relative_volume"]["percentile"] == 1.0
    assert result["metrics"]["relative_volume"]["notable"] is True
    assert "relative_volume" in result["notable_metrics"]
