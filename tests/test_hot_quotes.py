from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from runner_watch.market_data import EASTERN
from runner_web import db, quotes
from runner_web import main as web_main
from runner_web.db import connection, init_db

NOW = datetime(2026, 9, 2, 14, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def hot_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "hot.db")
    init_db()
    quotes._BUDGET_CALLS.clear()
    yield
    quotes._BUDGET_CALLS.clear()


def _scan(tickers_with_scores, captured_at=NOW):
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_runs(
                id,mode,label,feature_schema_version,requested_symbols,liquid_symbols,
                scanned_symbols,candidate_rows,failed_symbols_json,warnings_json,
                started_at,finished_at,captured_at
            ) VALUES('run','penny','Penny','test',9,9,9,?,'[]','[]',?,?,?)
            """,
            (
                len(tickers_with_scores),
                captured_at.isoformat(),
                captured_at.isoformat(),
                captured_at.isoformat(),
            ),
        )
        for rank, (ticker, score) in enumerate(tickers_with_scores, start=1):
            database.execute(
                """
                INSERT INTO scan_snapshots(
                    id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                    momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                    dollar_volume,quote_time,signals_json,risks_json,captured_at,
                    scan_run_id,baseline_rank,trade_state
                ) VALUES(?,?,?,'BUILDING','regular',1.0,4.0,1.0,2.0,3.0,3.0,0.5,
                         500000,?,'[]','[]',?,'run',?,'WATCH')
                """,
                (
                    f"snap-{ticker}",
                    ticker,
                    score,
                    captured_at.isoformat(),
                    captured_at.isoformat(),
                    rank,
                ),
            )


def _minute_frame(prices):
    index = pd.to_datetime(
        [f"2026-09-02 10:2{offset}:00" for offset in range(len(prices))]
    ).tz_localize(EASTERN)
    return pd.DataFrame(
        {
            "Open": prices,
            "High": prices,
            "Low": prices,
            "Close": prices,
            "Volume": [1_000] * len(prices),
        },
        index=index,
    )


class FakeClient:
    def __init__(self, frames):
        self.frames = frames
        self.calls = []

    def minutes(self, tickers, progress=None):
        self.calls.append(list(tickers))
        from runner_watch.market_data import DownloadResult

        return DownloadResult(
            frames={t: self.frames[t] for t in tickers if t in self.frames},
            failed=[t for t in tickers if t not in self.frames],
            warnings=[],
        )


def _install_client(monkeypatch, frames):
    client = FakeClient(frames)
    monkeypatch.setattr(quotes, "YahooMarketData", lambda **_kwargs: client)
    return client


def test_the_hot_set_is_the_top_of_the_board(monkeypatch):
    _scan([("AAA", 90), ("BBB", 80), ("CCC", 70), ("DDD", 60)])
    monkeypatch.setattr(web_main, "HOT_QUOTE_LIMIT", 2)

    assert web_main._hot_set() == ["AAA", "BBB"]


def test_the_hot_set_is_empty_without_a_scan():
    assert web_main._hot_set() == []


def test_one_batched_request_covers_the_whole_hot_set(monkeypatch):
    client = _install_client(
        monkeypatch,
        {"AAA": _minute_frame([1.0, 1.5]), "BBB": _minute_frame([2.0, 2.2])},
    )
    with connection() as database:
        for ticker, close in (("AAA", 1.0), ("BBB", 2.0)):
            database.execute(
                """
                INSERT INTO market_bars(
                    ticker,source,interval,bar_time,open,high,low,close,volume,
                    first_collected_at,last_collected_at
                ) VALUES(?,'yahoo','1d','2026-09-01T00:00:00+00:00',?,?,?,?,1000,?,?)
                """,
                (ticker, close, close, close, close, NOW.isoformat(), NOW.isoformat()),
            )

    result = quotes.refresh_hot_quotes(["AAA", "BBB"], at=NOW)

    assert result == {"requested": 2, "stored": 2, "missing": 0}
    assert client.calls == [["AAA", "BBB"]]
    marks = quotes.fresh_quotes(["AAA", "BBB"])
    assert marks["AAA"]["price"] == 1.5
    assert marks["AAA"]["previous_close"] == 1.0
    assert marks["AAA"]["change_pct"] == 50.0
    assert marks["BBB"]["change_pct"] == 10.0


def test_a_hot_refresh_without_a_previous_close_still_stores_a_price(monkeypatch):
    _install_client(monkeypatch, {"AAA": _minute_frame([1.0, 1.5])})

    result = quotes.refresh_hot_quotes(["AAA"], at=NOW)

    assert result["stored"] == 1
    mark = quotes.fresh_quotes(["AAA"])["AAA"]
    assert mark["price"] == 1.5
    assert mark["change_pct"] is None


def test_a_missing_frame_counts_as_missing(monkeypatch):
    _install_client(monkeypatch, {"AAA": _minute_frame([1.0])})

    result = quotes.refresh_hot_quotes(["AAA", "GONE"], at=NOW)

    assert result == {"requested": 2, "stored": 1, "missing": 1}


def test_the_hot_set_respects_the_budget(monkeypatch):
    client = _install_client(monkeypatch, {"AAA": _minute_frame([1.0])})
    monkeypatch.setattr(quotes, "QUOTE_CALLS_PER_MINUTE", 0)

    result = quotes.refresh_hot_quotes(["AAA"], at=NOW)

    assert result == {"requested": 1, "stored": 0, "missing": 1}
    assert client.calls == []


def test_the_hot_set_is_capped(monkeypatch):
    client = _install_client(monkeypatch, {})
    monkeypatch.setattr(quotes, "HOT_SET_LIMIT", 2)

    quotes.refresh_hot_quotes(["AAA", "BBB", "CCC", "DDD"], at=NOW)

    assert client.calls == [["AAA", "BBB"]]


def _row(**overrides):
    return {
        "ticker": "AAA",
        "price": 1.0,
        "change_pct": 4.0,
        "quote_time": (NOW - timedelta(minutes=6)).isoformat(),
        **overrides,
    }


def _store_quote(**overrides):
    values = {
        "ticker": "AAA",
        "price": 1.5,
        "observed_at": (NOW - timedelta(seconds=30)).isoformat(),
        "change_pct": 50.0,
        **overrides,
    }
    with connection() as database:
        database.execute(
            """
            INSERT INTO ticker_quotes(
                ticker,price,observed_at,session,previous_close,change_pct,
                source,status,requested_at,collected_at
            ) VALUES(?,?,?,'REGULAR',1.0,?,'yahoo','ok',?,?)
            """,
            (
                values["ticker"],
                values["price"],
                values["observed_at"],
                values["change_pct"],
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )


def test_a_fresher_mark_upgrades_a_board_row(monkeypatch):
    monkeypatch.setattr(web_main, "now", lambda: NOW)
    _store_quote()
    rows = [_row()]

    assert web_main._apply_market_marks(rows) == 1

    assert rows[0]["price"] == 1.5
    assert rows[0]["change_pct"] == 50.0
    assert rows[0]["mark_source"] == "quote"
    assert rows[0]["mark_age_seconds"] == 30


def test_a_mark_without_a_move_leaves_the_row_alone(monkeypatch):
    monkeypatch.setattr(web_main, "now", lambda: NOW)
    _store_quote(change_pct=None)
    rows = [_row()]

    assert web_main._apply_market_marks(rows) == 0

    assert rows[0]["price"] == 1.0
    assert rows[0]["change_pct"] == 4.0
    assert "mark_source" not in rows[0]


def test_a_mark_older_than_the_scan_is_ignored(monkeypatch):
    monkeypatch.setattr(web_main, "now", lambda: NOW)
    _store_quote(observed_at=(NOW - timedelta(minutes=30)).isoformat())
    rows = [_row()]

    assert web_main._apply_market_marks(rows) == 0
    assert rows[0]["price"] == 1.0


def test_a_mark_from_the_future_is_ignored(monkeypatch):
    monkeypatch.setattr(web_main, "now", lambda: NOW)
    _store_quote(observed_at=(NOW + timedelta(minutes=5)).isoformat())
    rows = [_row()]

    assert web_main._apply_market_marks(rows) == 0
    assert rows[0]["price"] == 1.0


def test_the_hot_quote_worker_is_registered() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "src/runner_web/main.py").read_text()

    assert 'name="hot-quotes"' in source
    assert "async def hot_quote_worker() -> None:" in source
