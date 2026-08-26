from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.short_data import (
    FintelShortDataClient,
    short_data_configured,
    short_data_for_scan,
)


def test_short_data_can_be_disabled_without_removing_the_key(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("FINTEL_API_KEY", "test-key")
    monkeypatch.setenv("FINTEL_SHORT_DATA_ENABLED", "false")

    assert short_data_configured() is False


def test_fintel_client_maps_short_interest_and_borrow_fields() -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str], _timeout: float) -> Any:
        calls.append(url)
        assert headers["X-API-KEY"] == "test-key"
        if url.endswith("/short-interest"):
            return {
                "data": [
                    {
                        "settlement_date": "2026-07-31",
                        "short_interest_percent_float": "12.5%",
                        "short_interest_shares": "1,200,000",
                        "days_to_cover": 1.8,
                    },
                    {
                        "settlementDate": "2026-08-15",
                        "shortInterestPctFloat": 18.25,
                        "sharesShort": 1_750_000,
                        "shortRatio": 2.4,
                    },
                ]
            }
        return {
            "data": {
                "borrowFeeRatePercent": 37.6,
                "sharesAvailableToBorrow": 85_000,
                "observedAt": "2026-08-25T19:45:00-04:00",
            }
        }

    result = FintelShortDataClient(
        "test-key",
        transport=transport,
        max_workers=1,
    ).fetch("pen")

    assert result.data.ticker == "PEN"
    assert result.data.short_interest_pct_float == 18.25
    assert result.data.short_interest_shares == 1_750_000
    assert result.data.days_to_cover == 2.4
    assert result.data.short_interest_settlement_date == "2026-08-15"
    assert result.data.borrow_fee_pct == 37.6
    assert result.data.shares_available == 85_000
    assert result.data.borrow_observed_at == "2026-08-25T19:45:00-04:00"
    assert result.data.source_url == "https://fintel.io/ss/us/pen"
    assert [fetch.status for fetch in result.fetches] == ["success", "success"]
    assert len(calls) == 2


def test_short_data_cache_avoids_repeat_api_calls(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "short-data.db")
    monkeypatch.delenv("FINTEL_API_KEY", raising=False)
    init_db()
    calls: list[str] = []

    def transport(url: str, _headers: dict[str, str], _timeout: float) -> Any:
        calls.append(url)
        if url.endswith("/short-interest"):
            return {
                "data": {
                    "settlement_date": "2026-08-15",
                    "short_interest_pct_float": 22.0,
                }
            }
        return {
            "data": {
                "observed_at": "2026-08-25T20:00:00-04:00",
                "borrow_fee_pct": 9.5,
                "shares_available": 250_000,
            }
        }

    client = FintelShortDataClient("test-key", transport=transport, max_workers=1)
    first = short_data_for_scan(
        ["PEN", "OTHER"],
        refresh_tickers=["PEN"],
        as_of=datetime(2026, 8, 25, 23, tzinfo=UTC),
        client=client,
    )
    second = short_data_for_scan(
        ["PEN", "OTHER"],
        refresh_tickers=["PEN"],
        as_of=datetime(2026, 8, 25, 23, 5, tzinfo=UTC),
        client=client,
    )

    assert first.configured is True
    assert first.refreshed == 1
    assert first.covered == 1
    assert first.rows["PEN"].short_interest_pct_float == 22.0
    assert second.refreshed == 0
    assert second.rows["PEN"].borrow_fee_pct == 9.5
    assert len(calls) == 2
    with connection() as database:
        cached = database.execute(
            "SELECT ticker,borrow_fee_pct,shares_available FROM short_data_cache"
        ).fetchone()
    assert tuple(cached) == ("PEN", 9.5, 250_000)
