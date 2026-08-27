import json
import traceback
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from runner_watch import massive_data
from runner_watch.market_data import routed_market_data
from runner_watch.massive_data import (
    MassiveAPIError,
    MassiveBarAdapter,
    MassiveClient,
    MassiveDailyCache,
    RateLimiter,
    _session_dates,
    backfill_daily_cache,
    massive_bar_adapter,
)
from runner_watch.provider_contracts import Bar, DataKind, FetchBatch, ProviderRequest

TODAY = date(2026, 8, 25)  # a Tuesday


def _grouped_body(*symbols: str) -> bytes:
    results = [
        {
            "T": symbol,
            "o": 1.0,
            "h": 1.5,
            "l": 0.9,
            "c": 1.2,
            "v": 1000.5,
            "vw": 1.1,
            "n": 10,
            "t": 1_700_000_000_000,
        }
        for symbol in symbols
    ]
    return json.dumps({"status": "OK", "results": results, "resultsCount": len(results)}).encode()


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


def _weekdays_back(days: int, today: date = TODAY) -> list[date]:
    start = today - timedelta(days=days)
    return [d for d in (start + timedelta(days=i) for i in range(days)) if d.weekday() < 5]


def test_rate_limiter_blocks_when_wait_is_too_long() -> None:
    limiter = RateLimiter(calls_per_minute=1)
    limiter.acquire(max_wait_seconds=0.0)
    with pytest.raises(massive_data.MassiveRateLimitError):
        limiter.acquire(max_wait_seconds=0.05)


def test_rate_limiter_recovers_after_refill() -> None:
    limiter = RateLimiter(calls_per_minute=600)  # refill is fast
    limiter.acquire(max_wait_seconds=0.0)
    limiter.acquire(max_wait_seconds=5.0)  # should not raise


def test_client_sends_api_key_and_parses_results(monkeypatch: MonkeyPatch) -> None:
    urls: list[str] = []
    fetches: list[object] = []

    def fake_urlopen(request, timeout=None):
        urls.append(request.full_url)
        return _FakeResponse(_grouped_body("AAA", "BBB"))

    monkeypatch.setattr(massive_data.urllib.request, "urlopen", fake_urlopen)
    client = MassiveClient(
        "test-key",
        limiter=RateLimiter(calls_per_minute=1000),
        fetch_recorder=fetches.append,
    )
    rows = client.grouped_daily(TODAY)

    assert [row["T"] for row in rows] == ["AAA", "BBB"]
    assert "apiKey=test-key" in urls[0]
    assert "adjusted=true" in urls[0]
    assert len(fetches) == 1
    fetch = fetches[0]
    assert fetch.source == "massive"
    assert fetch.status == "success"
    assert fetch.metadata["returned_rows"] == 2
    assert "test-key" not in fetch.locator
    assert "apiKey" not in fetch.locator


def test_client_raises_on_http_error(monkeypatch: MonkeyPatch) -> None:
    import urllib.error

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", hdrs=None, fp=BytesIO(b"{}")
        )

    monkeypatch.setattr(massive_data.urllib.request, "urlopen", fake_urlopen)
    client = MassiveClient("k", limiter=RateLimiter(calls_per_minute=1000))
    with pytest.raises(MassiveAPIError, match="HTTP 403"):
        client.grouped_daily(TODAY)


def test_client_redacts_api_key_from_network_errors(monkeypatch: MonkeyPatch) -> None:
    import urllib.error

    fetches: list[object] = []

    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(f"proxy rejected {request.full_url}")

    monkeypatch.setattr(massive_data.urllib.request, "urlopen", fake_urlopen)
    client = MassiveClient(
        "sensitive-key",
        limiter=RateLimiter(calls_per_minute=1000),
        fetch_recorder=fetches.append,
    )

    with pytest.raises(MassiveAPIError) as error:
        client.grouped_daily(TODAY)

    assert "sensitive-key" not in str(error.value)
    assert "sensitive-key" not in "".join(traceback.format_exception(error.value))
    assert "sensitive-key" not in fetches[0].error
    assert "sensitive-key" not in fetches[0].locator


def test_cache_stores_and_reads_bars(tmp_path: Path) -> None:
    with MassiveDailyCache(tmp_path / "sub" / "cache.sqlite3") as cache:
        session = TODAY - timedelta(days=1)
        cache.store_date(
            session,
            [
                {
                    "T": "aaa",
                    "c": 1.25,
                    "v": 500,
                    "o": 1.0,
                    "h": 1.3,
                    "l": 0.9,
                    "otc": False,
                },
                {"T": "OTCX", "c": 0.01, "otc": True},
            ],
        )
        assert session in cache.fetched_dates()
        bars = cache.bars_for({"AAA"})
        assert list(bars) == ["AAA"]
        assert bars["AAA"][0][1]["close"] == 1.25
        # OTC rows must not be stored.
        assert "OTCX" not in cache.bars_for({"OTCX"})


def test_cache_prunes_old_sessions(tmp_path: Path) -> None:
    with MassiveDailyCache(tmp_path / "cache.sqlite3") as cache:
        old = TODAY - timedelta(days=800)
        cache.store_date(old, [{"T": "AAA", "c": 1.0}])
        removed = cache.prune_before(TODAY - timedelta(days=730))
        assert removed >= 2  # one fetched_dates row plus one daily_bars row
        assert old not in cache.fetched_dates()


def _adapter_with_warm_cache(
    tmp_path: Path, symbols: set[str]
) -> tuple[MassiveBarAdapter, MassiveDailyCache, list[date]]:
    cache = MassiveDailyCache(tmp_path / "cache.sqlite3")
    sessions = _session_dates("1y", TODAY)
    for session in sessions:
        cache.store_date(
            session,
            [
                {"T": symbol, "c": 1.0 + index, "o": 1.0, "h": 2.0, "l": 0.5, "v": 100}
                for index, symbol in enumerate(sorted(symbols))
            ],
        )
    client = MassiveClient("k", limiter=RateLimiter(calls_per_minute=1000))
    return MassiveBarAdapter(client, cache, max_fetch_calls=10, today_et=TODAY), cache, sessions


def test_adapter_serves_daily_bars_from_cache_without_api_calls(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    adapter, cache, sessions = _adapter_with_warm_cache(tmp_path, {"AAA", "BBB"})

    def fail(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("cache was warm; no API call expected")

    monkeypatch.setattr(adapter.client, "grouped_daily", fail)
    request = ProviderRequest(
        kind=DataKind.BARS, symbols=("AAA", "BBB"), interval="1d", period="1y"
    )
    batch = adapter.fetch(request)

    assert batch.status == "success"
    assert batch.provenance.provider == "massive"
    assert batch.provenance.delayed is True
    assert {bar.symbol for bar in batch.bars} == {"AAA", "BBB"}
    assert len(batch.bars) == 2 * len(sessions)
    assert batch.provenance.quality["sessions_fetched_this_call"] == 0
    cache.close()


def test_adapter_fails_when_backfill_budget_is_exceeded(tmp_path: Path) -> None:
    cache = MassiveDailyCache(tmp_path / "cache.sqlite3")
    adapter = MassiveBarAdapter(
        MassiveClient("k", limiter=RateLimiter(calls_per_minute=1000)),
        cache,
        max_fetch_calls=0,
        today_et=TODAY,
    )
    request = ProviderRequest(kind=DataKind.BARS, symbols=("AAA",), interval="1d", period="1y")
    with pytest.raises(MassiveAPIError, match="scan budget"):
        adapter.fetch(request)
    cache.close()


def test_adapter_declines_intraday_requests(tmp_path: Path) -> None:
    cache = MassiveDailyCache(tmp_path / "cache.sqlite3")
    adapter = MassiveBarAdapter(
        MassiveClient("k", limiter=RateLimiter(calls_per_minute=1000)),
        cache,
        today_et=TODAY,
    )
    request = ProviderRequest(kind=DataKind.BARS, symbols=("AAA",), interval="5m", period="5d")
    with pytest.raises(ValueError, match="daily"):
        adapter.fetch(request)
    cache.close()


def test_adapter_reports_partial_when_symbol_is_missing(tmp_path: Path) -> None:
    adapter, cache, _ = _adapter_with_warm_cache(tmp_path, {"AAA"})
    request = ProviderRequest(
        kind=DataKind.BARS, symbols=("AAA", "ZZZ"), interval="1d", period="1y"
    )
    batch = adapter.fetch(request)
    assert batch.status == "partial"
    assert batch.provenance.quality["failed_symbols"] == ["ZZZ"]
    cache.close()


def test_client_treats_market_holiday_as_empty_session(monkeypatch: MonkeyPatch) -> None:
    """Massive returns status OK with null results on market holidays."""

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(json.dumps({"status": "OK", "results": None}).encode())

    monkeypatch.setattr(massive_data.urllib.request, "urlopen", fake_urlopen)
    client = MassiveClient("k", limiter=RateLimiter(calls_per_minute=1000))
    assert client.grouped_daily(TODAY) == []


def test_client_still_rejects_malformed_results(monkeypatch: MonkeyPatch) -> None:
    def fake_urlopen(request, timeout=None):
        return _FakeResponse(json.dumps({"status": "OK", "results": "nope"}).encode())

    monkeypatch.setattr(massive_data.urllib.request, "urlopen", fake_urlopen)
    client = MassiveClient("k", limiter=RateLimiter(calls_per_minute=1000))
    with pytest.raises(MassiveAPIError, match="not usable"):
        client.grouped_daily(TODAY)


def test_backfill_daily_cache_continues_past_holidays(tmp_path: Path) -> None:
    cache = MassiveDailyCache(tmp_path / "cache.sqlite3")

    class HolidayAwareClient(MassiveClient):
        def grouped_daily(self, session: date):
            if session.weekday() == 0:  # pretend every Monday is a holiday
                return []
            return [{"T": "AAA", "c": 1.0}]

    stats = backfill_daily_cache(
        HolidayAwareClient("k", limiter=RateLimiter(calls_per_minute=1000)),
        cache,
        days=14,
        today_et=TODAY,
        prune_days=None,
    )
    assert stats["sessions_fetched"] == stats["sessions_needed"]
    cache.close()


def test_refresh_massive_backfill_returns_disabled_without_key(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    assert massive_data.refresh_massive_backfill() == {"enabled": False}


def test_refresh_massive_backfill_respects_budget(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MASSIVE_CACHE_PATH", str(tmp_path / "cache.sqlite3"))
    monkeypatch.setenv("MASSIVE_BACKFILL_CALLS", "3")

    calls: list[date] = []

    class CountingClient(MassiveClient):
        def grouped_daily(self, session: date):
            calls.append(session)
            return [{"T": "AAA", "c": 1.0}]

    monkeypatch.setattr(
        massive_data, "massive_bar_adapter", lambda **kwargs: MassiveBarAdapter(
            CountingClient("k", limiter=RateLimiter(calls_per_minute=1000)),
            MassiveDailyCache(tmp_path / "cache.sqlite3"),
            max_fetch_calls=10,
            today_et=TODAY,
        ),
    )
    stats = massive_data.refresh_massive_backfill()
    assert stats["sessions_fetched"] == 3
    assert len(calls) == 3


def test_backfill_daily_cache_fetches_each_session_once(tmp_path: Path) -> None:
    cache = MassiveDailyCache(tmp_path / "cache.sqlite3")
    calls: list[date] = []

    class FakeClient(MassiveClient):
        def grouped_daily(self, session: date):
            calls.append(session)
            return [{"T": "AAA", "c": 1.0}]

    client = FakeClient("k", limiter=RateLimiter(calls_per_minute=1000))
    stats = backfill_daily_cache(client, cache, days=14, today_et=TODAY, prune_days=None)
    assert stats["sessions_fetched"] == stats["sessions_needed"]
    assert stats["sessions_cached_before"] == 0
    second = backfill_daily_cache(client, cache, days=14, today_et=TODAY, prune_days=None)
    assert second["sessions_fetched"] == 0
    assert second["sessions_cached_before"] == stats["sessions_needed"]
    assert len(calls) == stats["sessions_needed"]
    cache.close()


def test_massive_bar_adapter_returns_none_without_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    assert massive_bar_adapter() is None


class _FakeMassiveAdapter:
    name = "massive"
    capabilities = frozenset({DataKind.BARS})

    def __init__(self, bars: tuple[Bar, ...]) -> None:
        self.bars = bars
        self.closed = False

    def fetch(self, request, progress=None) -> FetchBatch:
        if request.interval != "1d":
            raise AssertionError("intraday requests must not reach massive")
        return FetchBatch(
            request=request,
            status="success",
            provenance=massive_data.ProviderProvenance(
                provider=self.name,
                feed="market_bars",
                locator="massive://test",
                as_of=datetime.now(UTC),
                collected_at=datetime.now(UTC),
                delayed=True,
            ),
            bars=self.bars,
        )

    def close(self) -> None:
        self.closed = True


def _reset_daily_frame_cache(monkeypatch: MonkeyPatch) -> None:
    from runner_watch import market_data

    monkeypatch.setattr(market_data, "_DAILY_CACHE_DAY", None)
    monkeypatch.setattr(market_data, "_DAILY_FRAME_CACHE", {})
    monkeypatch.setattr(market_data, "_DAILY_CACHE_PROVENANCE", None)


def test_routed_daily_prefers_massive_when_configured(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    from runner_watch import market_data

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    monkeypatch.setenv("MASSIVE_CACHE_PATH", str(tmp_path / "cache.sqlite3"))
    _reset_daily_frame_cache(monkeypatch)

    bar = Bar(
        symbol="AAA",
        interval="1d",
        timestamp=datetime(2026, 8, 22, 4, 0, tzinfo=UTC),
        close=1.5,
        volume=100,
    )
    monkeypatch.setattr(
        market_data,
        "massive_bar_adapter",
        lambda **kwargs: _FakeMassiveAdapter((bar,)),
    )
    result = routed_market_data(batch_size=1).daily(["aaa"])
    assert list(result.frames) == ["AAA"]
    assert result.provenance is not None
    assert result.provenance.provider == "massive"
    assert result.provenance.fallback_used is False


def test_routed_market_data_closes_massive_adapter(monkeypatch: MonkeyPatch) -> None:
    from runner_watch import market_data

    adapter = _FakeMassiveAdapter(())
    monkeypatch.setattr(market_data, "massive_bar_adapter", lambda **kwargs: adapter)

    with routed_market_data(batch_size=1):
        pass

    assert adapter.closed is True


def test_routed_intraday_stays_on_yahoo_when_massive_is_configured(
    monkeypatch: MonkeyPatch,
) -> None:
    import pandas as pd

    from runner_watch import market_data

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    index = pd.date_range("2026-08-25 09:30", periods=2, freq="5min")
    raw = pd.DataFrame({"Close": [1.0, 1.1], "Volume": [100, 120]}, index=index)
    monkeypatch.setattr(market_data.yf, "download", lambda **kwargs: raw)

    def surprise_fetch(request, progress=None):  # pragma: no cover - must not be reached
        raise AssertionError("massive must not be consulted for intraday bars")

    class GuardMassive:
        name = "massive"
        capabilities = frozenset({DataKind.BARS})

        def fetch(self, request, progress=None) -> FetchBatch:
            return surprise_fetch(request, progress)

    monkeypatch.setattr(market_data, "massive_bar_adapter", lambda **kwargs: GuardMassive())
    result = routed_market_data(batch_size=1).intraday(["aaa"])
    assert list(result.frames) == ["AAA"]
    assert result.provenance is not None
    assert result.provenance.attempted_providers == ("yahoo",)


def test_routed_daily_falls_back_to_yahoo_when_massive_errors(
    monkeypatch: MonkeyPatch,
) -> None:
    import pandas as pd

    from runner_watch import market_data

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    _reset_daily_frame_cache(monkeypatch)

    index = pd.date_range("2026-08-24", periods=2, freq="1D")
    raw = pd.DataFrame({"Close": [1.0, 1.1], "Volume": [100, 120]}, index=index)
    monkeypatch.setattr(market_data.yf, "download", lambda **kwargs: raw)

    class ExplodingMassive:
        name = "massive"
        capabilities = frozenset({DataKind.BARS})

        def fetch(self, request, progress=None) -> FetchBatch:
            raise MassiveAPIError("cache is cold")

    monkeypatch.setattr(market_data, "massive_bar_adapter", lambda **kwargs: ExplodingMassive())
    result = routed_market_data(batch_size=1).daily(["aaa"])
    assert list(result.frames) == ["AAA"]
    assert result.provenance is not None
    assert result.provenance.provider == "yahoo"
    assert result.provenance.attempted_providers == ("massive", "yahoo")
    assert result.provenance.fallback_used is True
    assert any("massive failed" in warning for warning in result.warnings)


def test_session_dates_exclude_today_and_weekends() -> None:
    # TODAY is Tuesday 2026-08-25; Monday and Friday must appear, Sunday not.
    sessions = _session_dates("5d", TODAY)
    assert sessions  # 5 calendar days back includes Friday + Monday
    assert TODAY not in sessions
    assert all(session.weekday() < 5 for session in sessions)
    assert max(sessions) == TODAY - timedelta(days=1)  # Monday
