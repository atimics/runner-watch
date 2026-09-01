from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from runner_node.tickers import (
    _nasdaq_ticker_results,
    frame_bars,
    load_ticker_detail,
    validate_ticker,
)
from runner_watch.market_data import DownloadResult
from runner_watch.provider_contracts import ProviderProvenance

TEST_MARKET_TIME = datetime(2026, 8, 28, 19, 0, tzinfo=UTC)


def _frame(*, daily: bool) -> pd.DataFrame:
    now = TEST_MARKET_TIME
    if daily:
        index = pd.date_range(end=now - timedelta(days=1), periods=30, freq="D")
    else:
        market_day = now.astimezone(ZoneInfo("America/New_York")) - timedelta(days=1)
        while market_day.weekday() >= 5:
            market_day -= timedelta(days=1)
        market_close = market_day.replace(hour=15, minute=55, second=0, microsecond=0)
        index = pd.date_range(end=market_close, periods=40, freq="5min")
    return pd.DataFrame(
        {
            "Open": [1 + number * 0.01 for number in range(len(index))],
            "High": [1.05 + number * 0.01 for number in range(len(index))],
            "Low": [0.95 + number * 0.01 for number in range(len(index))],
            "Close": [1.02 + number * 0.01 for number in range(len(index))],
            "Volume": [200_000 + number * 1_000 for number in range(len(index))],
        },
        index=index,
    )


def _result(symbol: str, *, daily: bool, provider: str = "yahoo") -> DownloadResult:
    now = TEST_MARKET_TIME
    return DownloadResult(
        frames={symbol: _frame(daily=daily)},
        failed=[],
        warnings=[],
        provenance=ProviderProvenance(
            provider=provider,
            feed="market_bars",
            locator="yfinance://download",
            as_of=now,
            observed_at=now,
            collected_at=now,
            delayed=True,
        ),
    )


class FakeProvider:
    def daily(self, symbols: list[str]) -> DownloadResult:
        return _result(symbols[0], daily=True)

    def intraday(self, symbols: list[str]) -> DownloadResult:
        return _result(symbols[0], daily=False)

    def close(self) -> None:
        pass


class IntradayOnlyProvider(FakeProvider):
    def daily(self, symbols: list[str]) -> DownloadResult:
        raise AssertionError(f"Daily fallback should not run for {symbols}")


class FailingIntradayProvider(IntradayOnlyProvider):
    def intraday(self, symbols: list[str]) -> DownloadResult:
        raise RuntimeError(f"Intraday unavailable for {symbols}")


def test_ticker_validation_rejects_paths_and_long_values() -> None:
    assert validate_ticker("brk.b") == "BRK-B"
    with pytest.raises(ValueError):
        validate_ticker("../../secret")
    with pytest.raises(ValueError):
        validate_ticker("A" * 20)


def test_frame_bars_are_json_safe_and_bounded() -> None:
    bars = frame_bars(_frame(daily=True), limit=4)

    assert len(bars) == 4
    assert bars[-1]["close"] is not None
    assert str(bars[-1]["timestamp"]).endswith("+00:00")


def test_local_ticker_detail_returns_charts_analysis_and_provenance(monkeypatch) -> None:
    monkeypatch.setattr(
        "runner_node.tickers._nasdaq_ticker_results",
        lambda _symbol: (_ for _ in ()).throw(LookupError("unavailable")),
    )
    monkeypatch.setattr("runner_node.tickers.routed_market_data", lambda **_kwargs: FakeProvider())

    detail = load_ticker_detail("test")

    assert detail["ticker"] == "TEST"
    assert detail["source"] == "local_scanner"
    assert detail["quote"]["price"] > 0  # type: ignore[index]
    assert detail["analysis"] is not None
    assert len(detail["charts"]["daily"]) == 30  # type: ignore[index]
    assert len(detail["charts"]["intraday"]) == 40  # type: ignore[index]
    assert [pull["provider"] for pull in detail["pulls"]] == ["yahoo", "yahoo"]  # type: ignore[index]
    assert all(pull["fallback_used"] for pull in detail["pulls"])  # type: ignore[index]


def test_local_ticker_detail_keeps_successful_nasdaq_results(monkeypatch) -> None:
    monkeypatch.setattr(
        "runner_node.tickers._nasdaq_ticker_results",
        lambda symbol: (
            _result(symbol, daily=True, provider="nasdaq"),
            _result(symbol, daily=False, provider="nasdaq"),
        ),
    )
    monkeypatch.setattr(
        "runner_node.tickers.routed_market_data",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("Fallback should not run")),
    )

    detail = load_ticker_detail("test")

    assert [pull["provider"] for pull in detail["pulls"]] == ["nasdaq", "nasdaq"]  # type: ignore[index]


def test_local_ticker_detail_falls_back_only_for_missing_interval(monkeypatch) -> None:
    missing_intraday = DownloadResult(
        frames={},
        failed=["TEST"],
        warnings=["Nasdaq intraday ticker source failed: timeout"],
    )
    monkeypatch.setattr(
        "runner_node.tickers._nasdaq_ticker_results",
        lambda symbol: (_result(symbol, daily=True, provider="nasdaq"), missing_intraday),
    )
    monkeypatch.setattr(
        "runner_node.tickers.routed_market_data",
        lambda **_kwargs: IntradayOnlyProvider(),
    )

    detail = load_ticker_detail("test")

    assert [pull["provider"] for pull in detail["pulls"]] == ["nasdaq", "yahoo"]  # type: ignore[index]
    assert detail["pulls"][1]["fallback_used"] is True  # type: ignore[index]
    assert detail["warnings"] == ["Nasdaq intraday ticker source failed: timeout"]


def test_local_ticker_detail_keeps_partial_data_when_fallback_fails(monkeypatch) -> None:
    missing_intraday = DownloadResult(frames={}, failed=["TEST"], warnings=[])
    monkeypatch.setattr(
        "runner_node.tickers._nasdaq_ticker_results",
        lambda symbol: (_result(symbol, daily=True, provider="nasdaq"), missing_intraday),
    )
    monkeypatch.setattr(
        "runner_node.tickers.routed_market_data",
        lambda **_kwargs: FailingIntradayProvider(),
    )

    detail = load_ticker_detail("test")

    assert len(detail["charts"]["daily"]) == 30  # type: ignore[index]
    assert detail["charts"]["intraday"] == []  # type: ignore[index]
    assert detail["pulls"][1]["status"] == "failed"  # type: ignore[index]
    assert detail["warnings"] == [
        "Market-data intraday fallback failed: Intraday unavailable for ['TEST']"
    ]


def test_nasdaq_ticker_results_use_real_price_points_without_inventing_volume(
    monkeypatch,
) -> None:
    def fake_json(path: str, _params: dict[str, str], **_kwargs):
        if path.endswith("/historical"):
            return {
                "tradesTable": {
                    "rows": [
                        {
                            "date": "08/27/2026",
                            "open": "$10.00",
                            "high": "$10.80",
                            "low": "$9.90",
                            "close": "$10.50",
                            "volume": "1,234,567",
                        },
                        {
                            "date": "08/26/2026",
                            "open": "$9.80",
                            "high": "$10.20",
                            "low": "$9.70",
                            "close": "$10.00",
                            "volume": "900,000",
                        },
                    ]
                }
            }
        return {
            "chart": [
                {"x": 1787891400000, "y": 10.0},
                {"x": 1787891460000, "y": 10.2},
                {"x": 1787891520000, "y": 9.9},
                {"x": 1787891700000, "y": 10.4},
            ]
        }

    monkeypatch.setattr("runner_node.tickers._nasdaq_json", fake_json)

    daily, intraday = _nasdaq_ticker_results("TEST")

    assert daily.provenance and daily.provenance.provider == "nasdaq"
    assert intraday.provenance and intraday.provenance.provider == "nasdaq"
    assert daily.frames["TEST"].iloc[-1]["Close"] == 10.5
    assert intraday.frames["TEST"].iloc[0]["High"] == 10.2
    assert intraday.frames["TEST"]["Volume"].isna().all()
    assert "does not publish volume" in intraday.warnings[0]
