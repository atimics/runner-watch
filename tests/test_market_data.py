import pandas as pd

from runner_watch.market_data import split_download_frame


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

