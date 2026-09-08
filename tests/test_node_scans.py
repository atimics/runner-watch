from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from runner_node.scans import ScanRequest, ScanStore, run_scan
from runner_watch.models import ScanSettings
from runner_watch.scanner import RunnerScanner
from tests.fake_market_data import FAKE_SYMBOLS, FakeMarketData


@pytest.mark.parametrize(
    "values",
    [
        {"min_price": 5, "max_price": 5},
        {"max_price": float("inf")},
        {"min_avg_dollar_volume": float("nan")},
        {"universe": "custom", "symbols": [" ", ","]},
        {"symbols": ["https://example.com"]},
        {"symbols": [" ".join(f"A{i}" for i in range(501))]},
        {"max_symbols": 5001},
        {"top_n": 101},
        {"sort": "unknown"},
    ],
)
def test_scan_filters_validate_at_node_boundary(values):
    with pytest.raises(ValidationError):
        ScanRequest(**values)


def test_custom_symbols_are_normalized_before_receipt():
    request = ScanRequest(universe="custom", symbols=[" aapl, BRK.B", "AAPL;nvda"])
    assert request.symbols == ["AAPL", "BRK-B", "NVDA"]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("score", ["HIGH", "LOW"]),
        ("volume", ["LOW", "HIGH"]),
        ("gainers", ["LOW", "HIGH"]),
        ("losers", ["HIGH", "LOW"]),
        ("price_asc", ["LOW", "HIGH"]),
        ("price_desc", ["HIGH", "LOW"]),
    ],
)
def test_rank_full_scan_before_result_limit_and_store_settings(
    monkeypatch, tmp_path, sort, expected
):
    now = datetime(2026, 8, 24, 10, 30, tzinfo=ZoneInfo("America/New_York"))
    result = RunnerScanner(FakeMarketData(now)).scan(
        FAKE_SYMBOLS, ScanSettings(min_avg_dollar_volume=0, top_n=1), now=now
    )
    high = replace(
        result.rows[0], ticker="HIGH", score=90, price=12, change_pct=-2, relative_volume=None
    )
    low = replace(high, ticker="LOW", score=10, price=2, change_pct=100, relative_volume=8)
    result = replace(result, rows=[high], all_rows=[high, low], liquid_symbols=3)
    provider = Mock()
    monkeypatch.setattr("runner_node.scans.routed_market_data", lambda **_kwargs: provider)
    scan = Mock(return_value=result)
    monkeypatch.setattr("runner_node.scans.RunnerScanner.scan", scan)
    request = ScanRequest(
        universe="custom",
        symbols=["high,low"],
        top_n=1,
        max_symbols=2,
        min_price=1,
        max_price=20,
        min_avg_volume=20,
        min_avg_dollar_volume=100,
        sort=sort,
    )

    receipt = run_scan(request)

    assert [row["ticker"] for row in receipt["rows"]] == expected[:1]
    assert receipt["matched_symbols"] == 2
    assert receipt["result_cap_reached"] is True
    assert receipt["scan_cap_reached"] is True
    assert receipt["request"] == request.model_dump()
    args = scan.call_args.args
    assert args[0] == ["HIGH", "LOW"]
    assert args[1].min_avg_volume == 20
    assert args[1].min_avg_dollar_volume == 100
    assert args[1].min_price == 1
    assert args[1].max_price == 20
    provider.close.assert_called_once()
    store = ScanStore(database_path=tmp_path / "scans.db")
    saved = store.save(receipt)
    assert store.get(saved["id"])["request"] == request.model_dump()


def test_provider_is_closed_when_scan_fails(monkeypatch):
    provider = Mock()
    monkeypatch.setattr("runner_node.scans.routed_market_data", lambda **_kwargs: provider)
    monkeypatch.setattr(
        "runner_node.scans.RunnerScanner.scan", Mock(side_effect=RuntimeError("Quote delay"))
    )
    with pytest.raises(RuntimeError, match="Quote delay"):
        run_scan(ScanRequest(universe="custom", symbols=["AAPL"]))
    provider.close.assert_called_once()
