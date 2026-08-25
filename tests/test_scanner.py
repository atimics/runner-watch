from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from runner_watch.chart_features import analyze_market_structure
from runner_watch.models import DailyProfile, ScanSettings
from runner_watch.sample_data import SAMPLE_SYMBOLS, SampleMarketData
from runner_watch.scanner import RunnerScanner, analyze_ticker, build_daily_profile

EASTERN = ZoneInfo("America/New_York")


def test_sample_scan_runs_end_to_end() -> None:
    now = datetime(2026, 8, 24, 10, 30, tzinfo=EASTERN)
    provider = SampleMarketData(now)
    result = RunnerScanner(provider).scan(
        SAMPLE_SYMBOLS,
        ScanSettings(min_avg_dollar_volume=0, max_symbols=20, top_n=20),
        now=now,
    )
    assert result.requested_symbols == len(SAMPLE_SYMBOLS)
    assert result.scanned_symbols == len(SAMPLE_SYMBOLS)
    assert len(result.rows) == len(SAMPLE_SYMBOLS)
    assert result.all_rows == result.rows
    assert result.rows == sorted(result.rows, key=lambda row: row.score, reverse=True)
    assert result.rows[0].relative_volume is not None
    assert result.rows[0].recent_dollar_volume > 0
    assert 0 <= result.rows[0].close_location <= 1
    assert result.rows[0].scoring_version == "market_risk_v3"
    assert result.rows[0].structure_available is True
    assert result.rows[0].support_strength >= 0
    assert result.rows[0].resistance_strength >= 0
    assert 0 <= result.rows[0].rug_score <= 100
    assert result.rows[0].trade_state in {"WATCH", "ARMED", "TRIGGERED", "AVOID"}
    assert any(row.stage == "EARLY" for row in result.rows)


def test_scan_keeps_complete_ranked_candidate_set() -> None:
    now = datetime(2026, 8, 24, 10, 30, tzinfo=EASTERN)
    result = RunnerScanner(SampleMarketData(now)).scan(
        SAMPLE_SYMBOLS,
        ScanSettings(min_avg_dollar_volume=0, max_symbols=20, top_n=2),
        now=now,
    )
    assert len(result.rows) == 2
    assert len(result.all_rows) == len(SAMPLE_SYMBOLS)
    assert result.rows == result.all_rows[:2]


def test_daily_profile_does_not_use_partial_current_day() -> None:
    now = datetime(2026, 8, 24, 10, 30, tzinfo=EASTERN)
    provider = SampleMarketData(now)
    frame = provider.daily(["SPRK"]).frames["SPRK"]
    frame.loc[frame.index[-1], "Close"] = 9999
    profile = build_daily_profile("SPRK", frame, now)
    assert profile is not None
    assert profile.previous_close != 9999


def test_chart_structure_has_gravity_and_fixed_fibonacci_anchors() -> None:
    index = pd.date_range("2026-08-24 09:30", periods=14, freq="5min", tz=EASTERN)
    close = [10, 10.2, 10.5, 11, 12, 11.5, 11, 11.4, 12, 13, 12.6, 12, 11.8, 12.1]
    frame = pd.DataFrame(
        {
            "Open": [value - 0.05 for value in close],
            "High": [value + 0.12 for value in close],
            "Low": [value - 0.12 for value in close],
            "Close": close,
            "Volume": [
                1000,
                1100,
                900,
                1300,
                3000,
                1400,
                2500,
                1200,
                1800,
                4000,
                1500,
                1300,
                2100,
                1600,
            ],
        },
        index=index,
    )

    analysis = analyze_market_structure(frame)

    assert analysis.features.structure_available is True
    assert analysis.features.fibonacci_available is True
    assert analysis.levels
    assert analysis.summary["opening_range"] is not None
    assert analysis.fibonacci is not None
    assert analysis.fibonacci["start"]["time"] < analysis.fibonacci["end"]["time"]
    assert [level["label"] for level in analysis.fibonacci["levels"]] == [
        "23.6%",
        "38.2%",
        "50.0%",
        "61.8%",
        "78.6%",
    ]


def test_scanner_structure_does_not_read_bars_after_scan_time() -> None:
    index = pd.date_range("2026-08-24 09:30", periods=12, freq="5min", tz=EASTERN)
    frame = pd.DataFrame(
        {
            "Open": [1.0] * 12,
            "High": [1.02] * 10 + [5.0, 6.0],
            "Low": [0.98] * 12,
            "Close": [1.0 + index * 0.005 for index in range(10)] + [5.0, 6.0],
            "Volume": [1000] * 10 + [1_000_000, 1_000_000],
        },
        index=index,
    )
    daily = DailyProfile(
        ticker="SAFE",
        previous_close=1.0,
        previous_high=1.1,
        average_volume=100_000,
        average_dollar_volume=100_000,
        high_20d=1.5,
        high_90d=2.0,
        high_52w=3.0,
        low_20d=0.8,
    )
    scan_time = datetime(2026, 8, 24, 10, 15, tzinfo=EASTERN)

    full = analyze_ticker("SAFE", daily, frame, scan_time)
    known = analyze_ticker("SAFE", daily, frame.iloc[:10], scan_time)

    assert full is not None and known is not None
    assert full.price == known.price
    assert full.session_volume == known.session_volume
    assert full.fib_retracement_pct == known.fib_retracement_pct
