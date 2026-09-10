from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from pytest import MonkeyPatch

from runner_watch.market_data import EASTERN, YahooQuoteAdapter, _last_print, session_label
from runner_watch.provider_contracts import (
    DataKind,
    FetchBatch,
    ProviderProvenance,
    ProviderRequest,
    Quote,
)
from runner_watch.provider_registry import ProviderRegistry
from runner_web import db, quotes
from runner_web.db import connection, init_db

TICKER = "PEN"
NOW = datetime(2026, 9, 2, 12, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def quote_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "quotes.db")
    init_db()
    quotes._BUDGET_CALLS.clear()
    quotes._INFLIGHT.clear()
    yield
    quotes._BUDGET_CALLS.clear()
    quotes._INFLIGHT.clear()


class FakeQuoteAdapter:
    name = "yahoo"
    capabilities = frozenset({DataKind.QUOTES})

    def __init__(self, quote: Quote | None, error: Exception | None = None) -> None:
        self.quote = quote
        self.error = error
        self.calls = 0

    def fetch(self, request: ProviderRequest, progress=None) -> FetchBatch:
        self.calls += 1
        if self.error:
            raise self.error
        collected_at = datetime.now(UTC)
        return FetchBatch(
            request=request,
            status="success" if self.quote else "error",
            provenance=ProviderProvenance(
                provider=self.name,
                feed="ticker_quote",
                locator="fake://quote",
                as_of=self.quote.observed_at if self.quote else collected_at,
                collected_at=collected_at,
            ),
            quotes=(self.quote,) if self.quote else (),
            error=None if self.quote else "no quote",
        )


def _quote(**overrides) -> Quote:
    return Quote(
        **{"symbol": TICKER, **overrides},
        observed_at=NOW - timedelta(minutes=1),
        last=1.5,
        previous_close=1.2,
        day_high=1.6,
        day_low=1.1,
        volume=42_000,
        session="REGULAR",
    )


def _install(monkeypatch: MonkeyPatch, adapter: FakeQuoteAdapter) -> FakeQuoteAdapter:
    def registry() -> ProviderRegistry:
        built = ProviderRegistry()
        built.register(adapter)
        built.route(DataKind.QUOTES, "yahoo")
        return built

    monkeypatch.setattr(quotes, "_quote_registry", registry)
    return adapter


def test_a_fetched_quote_is_stored_with_the_change_against_the_previous_close(monkeypatch):
    adapter = _install(monkeypatch, FakeQuoteAdapter(_quote()))

    quote = quotes.ticker_quote(TICKER, at=NOW)

    assert adapter.calls == 1
    assert quote["price"] == 1.5
    assert quote["previous_close"] == 1.2
    assert quote["change_pct"] == 25.0
    assert quote["session"] == "REGULAR"
    assert quote["fresh"] is True
    assert quote["age_seconds"] == 60
    assert quote["source"] == "yahoo"
    with connection() as database:
        stored = database.execute("SELECT COUNT(*) FROM ticker_quotes").fetchone()[0]
        snapshots = database.execute("SELECT COUNT(*) FROM scan_snapshots").fetchone()[0]
    assert stored == 1
    assert snapshots == 0


def test_a_second_read_inside_the_ttl_does_not_refetch(monkeypatch):
    adapter = _install(monkeypatch, FakeQuoteAdapter(_quote()))

    quotes.ticker_quote(TICKER, at=NOW)
    again = quotes.ticker_quote(TICKER, at=NOW + timedelta(seconds=quotes.QUOTE_TTL_SECONDS - 1))
    after = quotes.ticker_quote(TICKER, at=NOW + timedelta(seconds=quotes.QUOTE_TTL_SECONDS + 1))

    assert adapter.calls == 2
    assert again["price"] == 1.5
    assert after["price"] == 1.5


def test_the_budget_serves_the_stored_quote_instead_of_fetching(monkeypatch):
    adapter = _install(monkeypatch, FakeQuoteAdapter(_quote()))
    quotes.ticker_quote(TICKER, at=NOW)
    monkeypatch.setattr(quotes, "QUOTE_CALLS_PER_MINUTE", 1)

    later = quotes.ticker_quote(TICKER, at=NOW + timedelta(seconds=35))

    assert adapter.calls == 1
    assert later["price"] == 1.5


def test_a_failed_refresh_keeps_the_last_good_quote(monkeypatch):
    good = _install(monkeypatch, FakeQuoteAdapter(_quote()))
    quotes.ticker_quote(TICKER, at=NOW)
    assert good.calls == 1
    _install(monkeypatch, FakeQuoteAdapter(None, error=TimeoutError("slow")))

    later = quotes.ticker_quote(TICKER, at=NOW + timedelta(minutes=40))

    assert later["price"] == 1.5
    assert later["status"] == "ok"
    assert later["fresh"] is False
    with connection() as database:
        error = database.execute(
            "SELECT last_error FROM ticker_quotes WHERE ticker=?", (TICKER,)
        ).fetchone()[0]
    assert error == "ProvidersExhaustedError"


def test_a_provider_with_no_quote_is_recorded_without_a_price(monkeypatch):
    _install(monkeypatch, FakeQuoteAdapter(None))

    quote = quotes.ticker_quote(TICKER, at=NOW)

    assert quote["status"] == "error"
    assert quote["price"] is None
    assert quote["fresh"] is False


def test_a_response_for_another_symbol_is_treated_as_empty(monkeypatch):
    _install(monkeypatch, FakeQuoteAdapter(_quote(symbol="OTHER")))

    quote = quotes.ticker_quote(TICKER, at=NOW)

    assert quote["status"] == "empty"
    assert quote["price"] is None


def test_refresh_can_be_skipped_for_a_cheap_read(monkeypatch):
    adapter = _install(monkeypatch, FakeQuoteAdapter(_quote()))

    assert quotes.ticker_quote(TICKER, at=NOW, refresh=False) is None
    assert adapter.calls == 0


def test_the_adapter_reads_the_last_print_from_a_minute_frame():
    index = pd.to_datetime(
        ["2026-09-02 08:31:00", "2026-09-02 08:32:00", "2026-09-02 08:33:00"]
    ).tz_localize(EASTERN)
    frame = pd.DataFrame(
        {
            "Open": [1.0, 1.1, 1.2],
            "High": [1.05, 1.2, 1.35],
            "Low": [0.95, 1.05, 1.15],
            "Close": [1.02, 1.15, None],
            "Volume": [1_000, 2_000, 0],
        },
        index=index,
    )

    observed_at, values = _last_print(frame)

    assert observed_at == datetime(2026, 9, 2, 8, 32, tzinfo=EASTERN).astimezone(UTC)
    assert values["last"] == 1.15
    assert values["day_high"] == 1.2
    assert values["day_low"] == 0.95
    assert values["volume"] == 3_000
    assert _last_print(pd.DataFrame()) is None


def test_the_adapter_only_serves_quote_requests():
    adapter = YahooQuoteAdapter()

    with pytest.raises(ValueError):
        adapter.fetch(ProviderRequest(kind=DataKind.BARS, symbols=("PEN",), interval="5m"))


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 9, 2, 5, 0, tzinfo=EASTERN), "PRE-MARKET"),
        (datetime(2026, 9, 2, 10, 0, tzinfo=EASTERN), "REGULAR"),
        (datetime(2026, 9, 2, 17, 0, tzinfo=EASTERN), "AFTER-HOURS"),
        (datetime(2026, 9, 2, 21, 0, tzinfo=EASTERN), "CLOSED"),
        (datetime(2026, 9, 4, 10, 0, tzinfo=EASTERN), "REGULAR"),
        (datetime(2026, 9, 5, 10, 0, tzinfo=EASTERN), "CLOSED"),
    ],
)
def test_session_labels_cover_the_extended_day(moment, expected):
    assert session_label(moment) == expected


def test_the_quote_route_needs_a_known_ticker(monkeypatch):
    from starlette.testclient import TestClient

    from runner_web import main as web_main

    _install(monkeypatch, FakeQuoteAdapter(_quote()))
    client = TestClient(web_main.app, base_url=web_main.APP_ORIGIN)
    try:
        missing = client.get(f"/api/t/{TICKER}/quote")
        with connection() as database:
            database.execute(
                "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) "
                "VALUES(1,?,'Penny Labs','NASDAQ',?)",
                (TICKER, NOW.isoformat()),
            )
        found = client.get(f"/api/t/{TICKER}/quote")
    finally:
        client.close()

    assert missing.status_code == 404
    assert found.status_code == 200
    assert found.json()["price"] == 1.5
    assert "max-age" in found.headers["cache-control"]


class FakeFastInfo(dict):
    def __init__(self, previous_close, attribute: bool) -> None:
        super().__init__({} if attribute else {"previousClose": previous_close})
        self._attribute = attribute
        self._previous_close = previous_close

    @property
    def previous_close(self):
        if not self._attribute:
            raise AttributeError("previous_close")
        return self._previous_close


class FakeTicker:
    def __init__(self, frame, fast_info, error: Exception | None = None) -> None:
        self._frame = frame
        self.fast_info = fast_info
        self._error = error

    def history(self, **_kwargs):
        if self._error:
            raise self._error
        return self._frame


def _minute_frame() -> pd.DataFrame:
    index = pd.to_datetime(["2026-09-02 08:31:00", "2026-09-02 08:32:00"]).tz_localize(EASTERN)
    return pd.DataFrame(
        {
            "Open": [1.0, 1.1],
            "High": [1.05, 1.2],
            "Low": [0.95, 1.05],
            "Close": [1.02, 1.15],
            "Volume": [1_000, 2_000],
        },
        index=index,
    )


@pytest.mark.parametrize("attribute", [True, False])
def test_the_adapter_builds_a_canonical_quote_from_yahoo(monkeypatch, attribute):
    import runner_watch.market_data as market_data

    recorded = []
    monkeypatch.setattr(
        market_data.yf,
        "Ticker",
        lambda _symbol: FakeTicker(_minute_frame(), FakeFastInfo(1.0, attribute)),
    )
    adapter = YahooQuoteAdapter(fetch_recorder=recorded.append)

    batch = adapter.fetch(ProviderRequest(kind=DataKind.QUOTES, symbols=(TICKER,)))

    assert batch.status == "success"
    quote = batch.quotes[0]
    assert quote.symbol == TICKER
    assert quote.last == 1.15
    assert quote.previous_close == 1.0
    assert quote.session == "PRE-MARKET"
    assert quote.observed_at == datetime(2026, 9, 2, 8, 32, tzinfo=EASTERN).astimezone(UTC)
    assert batch.provenance.feed == "ticker_quote"
    assert batch.provenance.delayed is True
    assert [fetch.feed for fetch in recorded] == ["ticker_quote"]


def test_the_adapter_records_a_failure_when_yahoo_breaks(monkeypatch):
    import runner_watch.market_data as market_data

    recorded = []
    monkeypatch.setattr(
        market_data.yf,
        "Ticker",
        lambda _symbol: FakeTicker(None, FakeFastInfo(1.0, True), error=OSError("network")),
    )
    adapter = YahooQuoteAdapter(fetch_recorder=recorded.append)

    batch = adapter.fetch(ProviderRequest(kind=DataKind.QUOTES, symbols=(TICKER,)))

    assert batch.status == "error"
    assert batch.quotes == ()
    assert batch.provenance.quality["failed_symbols"] == [TICKER]
    assert any("network" in warning for warning in batch.provenance.warnings)
    assert [fetch.status for fetch in recorded] == ["error"]


def test_a_missing_previous_close_still_yields_a_quote(monkeypatch):
    import runner_watch.market_data as market_data

    monkeypatch.setattr(
        market_data.yf,
        "Ticker",
        lambda _symbol: FakeTicker(_minute_frame(), FakeFastInfo(None, True)),
    )
    adapter = YahooQuoteAdapter()

    batch = adapter.fetch(ProviderRequest(kind=DataKind.QUOTES, symbols=(TICKER,)))

    assert batch.quotes[0].previous_close is None
    assert batch.quotes[0].last == 1.15
    assert "Yahoo did not report a previous close" in batch.provenance.warnings
