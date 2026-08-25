import pandas as pd
from pytest import MonkeyPatch

from runner_watch import market_data
from runner_watch.market_data import YahooMarketData, routed_market_data, split_download_frame


def test_split_download_frame_handles_ticker_first_columns() -> None:
    index = pd.date_range("2026-08-24 09:30", periods=2, freq="5min")
    columns = pd.MultiIndex.from_product([["AAA", "BBB"], ["Close", "Volume"]])
    raw = pd.DataFrame([[1, 10, 2, 20], [1.1, 12, 2.1, 22]], index=index, columns=columns)
    result = split_download_frame(raw, ["AAA", "BBB"])
    assert set(result) == {"AAA", "BBB"}
    assert result["AAA"]["Close"].iloc[-1] == 1.1


def test_split_download_frame_handles_price_first_columns() -> None:
    index = pd.date_range("2026-08-24 09:30", periods=2, freq="5min")
    columns = pd.MultiIndex.from_product([["Close", "Volume"], ["AAA", "BBB"]])
    raw = pd.DataFrame([[1, 2, 10, 20], [1.1, 2.1, 12, 22]], index=index, columns=columns)
    result = split_download_frame(raw, ["AAA", "BBB"])
    assert result["BBB"]["Volume"].iloc[-1] == 22


def test_split_download_frame_handles_one_flat_ticker() -> None:
    raw = pd.DataFrame({"Close": [1.0], "Volume": [100]})
    result = split_download_frame(raw, ["AAA"])
    assert list(result) == ["AAA"]


def test_yahoo_download_emits_a_shared_source_fetch(monkeypatch: MonkeyPatch) -> None:
    raw = pd.DataFrame({"Close": [1.0], "Volume": [100]})
    monkeypatch.setattr(market_data.yf, "download", lambda **kwargs: raw)
    fetches = []
    result = YahooMarketData(batch_size=1, fetch_recorder=fetches.append).intraday(["AAA"])
    assert list(result.frames) == ["AAA"]
    assert len(fetches) == 1
    assert fetches[0].source == "yahoo"
    assert fetches[0].feed == "market_bars"
    assert fetches[0].status == "success"
    assert fetches[0].metadata["requested_tickers"] == ["AAA"]


def test_routed_market_data_returns_provider_provenance(monkeypatch: MonkeyPatch) -> None:
    index = pd.date_range("2026-08-25 09:30", periods=2, freq="5min")
    raw = pd.DataFrame(
        {"Open": [1.0, 1.1], "High": [1.2, 1.3], "Low": [0.9, 1.0],
         "Close": [1.1, 1.2], "Volume": [100, 120]},
        index=index,
    )
    monkeypatch.setattr(market_data.yf, "download", lambda **kwargs: raw)

    result = routed_market_data(batch_size=1).intraday(["aaa"])

    assert list(result.frames) == ["AAA"]
    assert result.provenance is not None
    assert result.provenance.provider == "yahoo"
    assert result.provenance.attempted_providers == ("yahoo",)
    assert result.provenance.fallback_used is False
