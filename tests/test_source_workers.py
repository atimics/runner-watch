from __future__ import annotations

import asyncio

import pytest
from pytest import MonkeyPatch

from runner_web import source_workers


class WorkerStopped(Exception):
    pass


def stop_after_initial_delay(monkeypatch: MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) > 1:
            raise WorkerStopped

    monkeypatch.setattr(source_workers.asyncio, "sleep", sleep)
    return delays


def test_discovery_worker_runs_each_enabled_source_once(monkeypatch: MonkeyPatch) -> None:
    delays = stop_after_initial_delay(monkeypatch)
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(source_workers, "discovery_sources_enabled", lambda: True)
    monkeypatch.setattr(
        source_workers,
        "discovery_watchlist",
        lambda limit: [{"ticker": "ONE", "company": "One Corp"}],
    )
    monkeypatch.setattr(source_workers, "yahoo_news_enabled", lambda: True)
    monkeypatch.setattr(source_workers, "gdelt_news_enabled", lambda: True)
    monkeypatch.setattr(source_workers, "bluesky_search_enabled", lambda: True)
    monkeypatch.setattr(
        source_workers,
        "refresh_yahoo_news",
        lambda ticker, company: calls.append(("yahoo", ticker, company)),
    )
    monkeypatch.setattr(
        source_workers,
        "refresh_gdelt_news",
        lambda ticker, company: calls.append(("gdelt", ticker, company)),
    )
    monkeypatch.setattr(
        source_workers,
        "refresh_bluesky_social",
        lambda ticker: calls.append(("bluesky", ticker)),
    )

    with pytest.raises(WorkerStopped):
        asyncio.run(source_workers.discovery_source_worker())

    assert sorted(calls) == sorted(
        [
            ("yahoo", "ONE", "One Corp"),
            ("gdelt", "ONE", "One Corp"),
            ("bluesky", "ONE"),
        ]
    )
    assert delays[0] == 25
    assert 5 <= delays[1] <= source_workers.DISCOVERY_INTERVAL_SECONDS


def test_apewisdom_worker_uses_the_full_watchlist(monkeypatch: MonkeyPatch) -> None:
    delays = stop_after_initial_delay(monkeypatch)
    watchlist = [
        {"ticker": "ONE", "company": "One Corp"},
        {"ticker": "TWO", "company": "Two Corp"},
    ]
    refreshed: list[list[dict[str, str]]] = []
    monkeypatch.setattr(source_workers, "apewisdom_social_enabled", lambda: True)
    monkeypatch.setattr(source_workers, "discovery_watchlist", lambda limit: watchlist)
    monkeypatch.setattr(
        source_workers,
        "refresh_apewisdom_social",
        lambda values: refreshed.append(values),
    )

    with pytest.raises(WorkerStopped):
        asyncio.run(source_workers.apewisdom_source_worker())

    assert refreshed == [watchlist]
    assert delays == [15, 900]


def test_halt_worker_logs_a_refresh_failure_and_keeps_running(
    monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    delays: list[float] = []

    async def stop_sleep(delay: float) -> None:
        delays.append(delay)
        raise WorkerStopped

    def fail_refresh() -> None:
        raise RuntimeError("feed unavailable")

    monkeypatch.setattr(source_workers, "trade_halts_enabled", lambda: True)
    monkeypatch.setattr(source_workers, "extended_us_session_is_open", lambda: True)
    monkeypatch.setattr(source_workers, "refresh_trade_halts", fail_refresh)
    monkeypatch.setattr(source_workers.asyncio, "sleep", stop_sleep)

    with caplog.at_level("WARNING"), pytest.raises(WorkerStopped):
        asyncio.run(source_workers.trading_halt_worker())

    assert delays == [60]
    assert "Nasdaq halt refresh failed: feed unavailable" in caplog.text


def test_house_disclosures_enabled_accepts_explicit_true(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("HOUSE_DISCLOSURES_ENABLED", "true")

    assert source_workers.house_disclosures_enabled() is True


def test_house_disclosure_worker_refreshes_the_free_feed(monkeypatch: MonkeyPatch) -> None:
    delays = stop_after_initial_delay(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(source_workers, "house_disclosures_enabled", lambda: True)
    monkeypatch.setattr(
        source_workers,
        "refresh_house_disclosures",
        lambda: calls.append("house"),
    )

    with pytest.raises(WorkerStopped):
        asyncio.run(source_workers.house_disclosure_worker())

    assert calls == ["house"]
    assert delays[0] == 35
    assert 30 <= delays[1] <= source_workers.HOUSE_DISCLOSURE_INTERVAL_SECONDS
