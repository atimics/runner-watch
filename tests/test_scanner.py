from datetime import datetime
from zoneinfo import ZoneInfo

from runner_watch.models import ScanSettings
from runner_watch.sample_data import SAMPLE_SYMBOLS, SampleMarketData
from runner_watch.scanner import RunnerScanner, build_daily_profile

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
    assert result.rows == sorted(result.rows, key=lambda row: row.score, reverse=True)
    assert result.rows[0].relative_volume is not None
    assert any(row.stage == "EARLY" for row in result.rows)


def test_daily_profile_does_not_use_partial_current_day() -> None:
    now = datetime(2026, 8, 24, 10, 30, tzinfo=EASTERN)
    provider = SampleMarketData(now)
    frame = provider.daily(["SPRK"]).frames["SPRK"]
    frame.loc[frame.index[-1], "Close"] = 9999
    profile = build_daily_profile("SPRK", frame, now)
    assert profile is not None
    assert profile.previous_close != 9999
