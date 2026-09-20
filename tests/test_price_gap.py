"""The gap dataset: what price was likely doing while bars were missing.

The projection is the honest dashed stretch on the chart and, once the missing
bars land, the labelled answer it gets scored against. These tests pin the two
halves together: a prediction is written once, never rewritten, and resolution
records what actually printed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from runner_web import db, price_gap

UTC_TUESDAY = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)  # Tue 11:00 ET · regular session


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "price-gap.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    with db.connection() as connection:
        yield connection


def test_chart_payload_carries_the_gap_and_honest_freshness(database):
    """The chart says how old its last point is instead of implying it is now."""

    from runner_web import main as web_main

    anchor = datetime.now(UTC) - timedelta(days=1)
    add_bars(
        database,
        "TEST",
        [(anchor, 10.0, 100), (anchor + timedelta(minutes=5), 10.2, 120)],
    )
    payload = web_main._ticker_chart_detail_payload_uncached("TEST")
    assert payload["freshness"]["stale"] is True
    assert payload["freshness"]["as_of"] == (anchor + timedelta(minutes=5)).isoformat()
    assert payload["freshness"]["latency_seconds"] > 300
    gap = payload["gap"]
    assert gap and gap["state"] == "stale"
    assert gap["anchor_price"] == 10.2
    assert gap["path"][-1]["time"] > gap["anchor_time"]
    assert "projection" in gap["label"]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def add_bars(database, ticker: str, rows: list[tuple[datetime, float, float]]) -> None:
    with database as conn:
        conn.executemany(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,open,high,low,close,volume,
                first_collected_at,last_collected_at
            ) VALUES('yahoo',?, '5m',?,?,?,?,?,?,?,?)
            """,
            [
                (
                    ticker, _iso(moment), price, price, price, price,
                    volume, _iso(moment), _iso(moment),
                )
                for moment, price, volume in rows
            ],
        )


def add_quote(database, ticker: str, moment: datetime, price: float, *, status: str = "ok") -> None:
    with database as conn:
        conn.execute(
            """
            INSERT INTO ticker_quotes(
                ticker,price,observed_at,session,previous_close,change_pct,day_high,day_low,
                volume,source,status,last_error,requested_at,collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ticker,
                price,
                _iso(moment),
                "regular",
                price,
                0.0,
                price,
                price,
                1.0,
                "yahoo",
                status,
                None,
                _iso(moment),
                _iso(moment),
            ),
        )


def add_scan(database, tickers: list[tuple[str, float]], *, captured: datetime) -> None:
    run_id = f"run-{_iso(captured)}"
    with database as conn:
        conn.execute(
            """
            INSERT INTO scan_runs(
                id,mode,label,feature_schema_version,requested_symbols,liquid_symbols,
                scanned_symbols,candidate_rows,failed_symbols_json,warnings_json,
                started_at,finished_at,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                "test",
                "Test",
                "1",
                len(tickers),
                len(tickers),
                len(tickers),
                len(tickers),
                "[]",
                "[]",
                _iso(captured),
                _iso(captured),
                _iso(captured),
            ),
        )
        for index, (ticker, score) in enumerate(tickers):
            conn.execute(
                """
                INSERT INTO scan_snapshots(
                    id,scan_run_id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                    momentum_15m_pct,breakout_pct,dollar_volume,quote_time,
                    signals_json,risks_json,captured_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"{run_id}-{index}",
                    run_id,
                    ticker,
                    score,
                    "candidate",
                    "regular",
                    10.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    1_000.0,
                    _iso(captured),
                    "[]",
                    "[]",
                    _iso(captured),
                ),
            )


def test_forecast_path_is_flat_with_a_widening_band():
    path = price_gap.forecast_path(100.0, 0.01, 15)
    assert [point["step"] for point in path] == [1, 2, 3]
    assert all(point["price"] == 100.0 for point in path)
    assert all(point["low"] < 100.0 < point["high"] for point in path)
    assert path[0]["high"] < path[1]["high"] < path[2]["high"]
    assert path[0]["low"] > path[1]["low"] > path[2]["low"]


def test_forecast_path_is_deterministic():
    assert price_gap.forecast_path(12.5, 0.004, 30) == price_gap.forecast_path(12.5, 0.004, 30)


def test_step_volatility_uses_ewma_and_never_returns_zero():
    assert price_gap.step_volatility([]) == price_gap.VOLATILITY_FLOOR
    assert price_gap.step_volatility([0.0, 0.0]) == price_gap.VOLATILITY_FLOOR
    assert price_gap.step_volatility([0.02, 0.02]) > price_gap.VOLATILITY_FLOOR


@pytest.mark.parametrize(
    ("latency", "expected"),
    [
        (60, "live"),
        (300, "live"),
        (600, "lagging"),
        (1_000, "stale"),
    ],
)
def test_gap_state_follows_the_bar_cadence(latency, expected):
    assert price_gap.gap_state(latency, session="regular", scanner_active=True) == expected
    assert price_gap.gap_state(latency, session="closed", scanner_active=False) == "closed"


def test_anchor_prefers_the_newer_quote(database):
    add_bars(database, "TEST", [(UTC_TUESDAY, 10.0, 100)])
    add_quote(database, "TEST", UTC_TUESDAY + timedelta(minutes=3), 10.5)
    anchor = price_gap.anchor_for(database, "TEST")
    assert anchor and anchor["kind"] == "quote" and anchor["price"] == 10.5


def test_forecasts_are_recorded_once_per_anchor(database):
    add_bars(database, "TEST", [(UTC_TUESDAY, 10.0, 100)])
    later = UTC_TUESDAY + timedelta(minutes=12)
    assert price_gap.record_forecasts(database, tickers=["TEST"], at=later) == 1
    with database as conn:
        first = dict(
            conn.execute(
                "SELECT latency_seconds,predicted_path_json,horizon_minutes,state "
                "FROM price_gap_forecasts WHERE ticker='TEST'"
            ).fetchone()
        )
    assert first["state"] == "lagging"
    assert first["latency_seconds"] == 720
    # A later pass for the same anchor must not rewrite the standing prediction.
    later_latency = later + timedelta(minutes=20)
    assert price_gap.record_forecasts(database, tickers=["TEST"], at=later_latency) == 0
    with database as conn:
        rows = conn.execute("SELECT COUNT(*) AS total FROM price_gap_forecasts").fetchone()
        second = dict(
            conn.execute(
                "SELECT latency_seconds,predicted_path_json FROM price_gap_forecasts "
                "WHERE ticker='TEST'"
            ).fetchone()
        )
    assert rows["total"] == 1
    assert second == {
        "latency_seconds": first["latency_seconds"],
        "predicted_path_json": first["predicted_path_json"],
    }


def test_closed_market_records_the_weekend_gap(database):
    """Bars stop on purpose at the weekend; those long flats are the examples
    worth the most, so they are recorded and labelled as closed, not late."""

    saturday = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
    add_bars(database, "TEST", [(saturday - timedelta(days=1), 10.0, 100)])
    assert price_gap.record_forecasts(database, tickers=["TEST"], at=saturday) == 1
    with database as conn:
        row = dict(
            conn.execute(
                "SELECT state,market_open,latency_seconds FROM price_gap_forecasts "
                "WHERE ticker='TEST'"
            ).fetchone()
        )
    assert row == {"state": "stale", "market_open": 0, "latency_seconds": 86_400}
    assert price_gap.refresh_price_gaps(database, at=saturday)["recorded"] == 0


def test_gap_projection_is_silent_while_the_bars_are_current(database):
    add_bars(database, "TEST", [(UTC_TUESDAY, 10.0, 100)])
    assert price_gap.gap_projection(database, "TEST", at=UTC_TUESDAY + timedelta(seconds=5)) is None


def test_gap_projection_reaches_the_clock_with_a_widening_band(database):
    add_bars(
        database,
        "TEST",
        [
            (UTC_TUESDAY + timedelta(minutes=5 * index), 10.0 + index * 0.05, 100)
            for index in range(6)
        ],
    )
    at = UTC_TUESDAY + timedelta(minutes=45)
    projection = price_gap.gap_projection(database, "TEST", at=at)
    assert projection["anchor_time"] == (UTC_TUESDAY + timedelta(minutes=25)).isoformat()
    assert projection["market_open"] is True
    assert projection["path"][-1]["time"] == at.isoformat()
    assert all(point["price"] == projection["anchor_price"] for point in projection["path"])
    assert projection["path"][-1]["high"] > projection["path"][0]["high"]
    assert projection["path"][-1]["low"] < projection["path"][0]["low"]
    assert "projection" in projection["label"]
    # Deterministic, so every instance and repeat draws the same dashed line.
    assert projection == price_gap.gap_projection(database, "TEST", at=at)


def test_gap_projection_says_so_when_the_market_is_closed(database):
    friday_close = datetime(2026, 9, 19, 1, 0, tzinfo=UTC)  # Fri 21:00 ET
    add_bars(database, "TEST", [(friday_close, 10.0, 100)])
    projection = price_gap.gap_projection(
        database, "TEST", at=friday_close + timedelta(days=2)
    )
    assert projection["market_open"] is False
    assert projection["state"] == "stale"
    assert projection["gap_minutes"] == 2_880
    assert projection["label"].startswith("Market closed")
    assert projection["band_pct"] > 0
    assert len(projection["path"]) <= 96


def test_resolution_scores_the_projection_against_the_bars_that_arrived(database):
    anchor = UTC_TUESDAY
    add_bars(database, "TEST", [(anchor, 10.0, 100)])
    at = anchor + timedelta(minutes=15)
    assert price_gap.record_forecasts(database, tickers=["TEST"], at=at) == 1
    assert price_gap.resolve_forecasts(database, at=at) == 0  # the gap is still open
    add_bars(
        database,
        "TEST",
        [
            (anchor + timedelta(minutes=5), 10.1, 100),
            (anchor + timedelta(minutes=10), 9.9, 100),
            (anchor + timedelta(minutes=15), 10.2, 100),
        ],
    )
    assert price_gap.resolve_forecasts(database, at=at + timedelta(minutes=5)) == 1
    with database as conn:
        row = dict(
            conn.execute(
                "SELECT resolved_at,actual_path_json,pct_error,max_pct_error,covered "
                "FROM price_gap_forecasts WHERE ticker='TEST'"
            ).fetchone()
        )
    actual = json.loads(row["actual_path_json"])
    assert [point["price"] for point in actual] == [10.1, 9.9, 10.2]
    assert row["pct_error"] == pytest.approx((0.1 + 0.1 + 0.2) / 3 / 10, abs=1e-6)
    assert row["max_pct_error"] == pytest.approx(0.02, abs=1e-6)
    assert row["covered"] in (0, 1)
    assert row["resolved_at"] is not None
    # Resolving again is a no-op, so an outcome is never scored twice.
    assert price_gap.resolve_forecasts(database, at=at + timedelta(minutes=10)) == 0


def test_tracked_tickers_follow_the_latest_scan(database):
    add_scan(database, [("LOW", 1.0), ("HIGH", 9.0)], captured=UTC_TUESDAY)
    add_scan(database, [("NEWEST", 5.0)], captured=UTC_TUESDAY + timedelta(minutes=30))
    assert price_gap.tracked_tickers(database) == ["NEWEST"]


def test_gap_summary_reports_the_honest_scoreboard(database):
    add_bars(database, "TEST", [(UTC_TUESDAY, 10.0, 100)])
    at = UTC_TUESDAY + timedelta(minutes=5)
    price_gap.record_forecasts(database, tickers=["TEST"], at=at)
    add_bars(database, "TEST", [(at, 10.0, 100)])
    price_gap.resolve_forecasts(database, at=at + timedelta(minutes=5))
    summary = price_gap.gap_summary(database)
    assert summary["total"] == 1 and summary["resolved"] == 1
    assert summary["mean_pct_error"] == pytest.approx(0.0, abs=1e-9)
    assert summary["coverage"] == 1.0
