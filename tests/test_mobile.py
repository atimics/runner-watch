from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.main import (
    _evidence_gate,
    _market_trade_pressure,
    _pulse_label,
    pulse_data,
    radar_data,
    ticker_detail_data,
)


def insert_filing(
    accession: str,
    ticker: str,
    price: float,
    score: float,
    filed_at: str,
    transaction_codes: str = "",
) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                filing_url,transaction_codes,price,change_pct,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                accession,
                1,
                ticker,
                f"{ticker} Company",
                "4",
                "Insider open-market buy",
                "positive",
                score,
                f"4 - {ticker} Company",
                filed_at,
                f"https://www.sec.gov/{accession}",
                transaction_codes,
                price,
                6.5,
                filed_at,
                filed_at,
            ),
        )


def test_pulse_only_lists_penny_stocks_and_groups_events(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "mobile.db")
    init_db()
    filed_at = datetime.now(UTC).isoformat()
    insert_filing("penny-one", "PEN", 2.25, 80, filed_at, "P")
    insert_filing("penny-two", "PEN", 2.25, 70, filed_at, "P")
    insert_filing("big-one", "BIG", 42.0, 99, filed_at, "P")
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "big-snapshot",
                "HUGE",
                100,
                "BUILDING",
                "regular",
                60.0,
                12.0,
                2.0,
                5.0,
                4.0,
                5.0,
                1.0,
                2_000_000,
                filed_at,
                "[]",
                "[]",
                filed_at,
            ),
        )

    result = pulse_data()

    assert [row["ticker"] for row in result["rows"]] == ["PEN"]
    assert result["rows"][0]["event_count"] == 2
    assert result["rows"][0]["evidence_gate"]["checks"] == ["Positive SEC catalyst"]
    assert result["stats"]["filings"] == 2


def test_ticker_detail_explains_form_four_purchase(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "detail.db")
    init_db()
    filed_at = datetime.now(UTC).isoformat()
    insert_filing("detail-one", "PEN", 1.75, 77, filed_at, "P")

    detail = ticker_detail_data("PEN")

    assert detail is not None
    assert detail["events"][0]["evidence_label"] == "Verified insider purchase"
    assert detail["events"][0]["pulse_label"] == "Form 4 · insider buy"


def test_pulse_label_does_not_call_a_sale_a_buy() -> None:
    assert _pulse_label({"transaction_codes": "S", "actor_title": "CEO"}) == (
        "Form 4 · insider sale"
    )


def test_pulse_puts_market_runners_before_filing_only_events(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ordered.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("filing-only", "FILE", 2.0, 99, captured_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) VALUES(?,?,?,?,?)",
            (1, "RUN", "Runner Systems", "NASDAQ", captured_at),
        )
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "runner", "RUN", 24, "BUILDING", "regular", 1.25, 13.0, 2.0,
                4.0, 4.2, 5.0, 1.0, 800_000, captured_at,
                '["Volume acceleration"]', '["Wide spread risk"]', captured_at,
            ),
        )

    result = pulse_data()

    assert [row["ticker"] for row in result["rows"]] == ["RUN", "FILE"]
    assert result["rows"][0]["source"] == "market"
    assert result["rows"][0]["company"] == "Runner Systems"
    assert result["rows"][0]["evidence_gate"]["state"] == "ready"
    assert result["rows"][1]["source"] == "sec"


def test_trade_pressure_is_an_honest_bar_derived_estimate(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "pressure.db")
    init_db()
    with connection() as database:
        for index in range(12):
            database.execute(
                """
                INSERT INTO market_bars(
                    source,ticker,interval,bar_time,open,high,low,close,volume,
                    first_collected_at,last_collected_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "yahoo",
                    "BUY",
                    "5m",
                    f"2026-08-24T14:{index * 5:02d}:00+00:00",
                    10.0,
                    11.0,
                    9.0,
                    10.8,
                    1000 + index * 100,
                    "2026-08-24T15:00:00+00:00",
                    "2026-08-24T15:00:00+00:00",
                ),
            )

    pressure = _market_trade_pressure("BUY")

    assert pressure["available"] is True
    assert pressure["buy_pressure_pct"] == 90.0
    assert pressure["delta_volume"] > 0
    assert pressure["bar_count"] == 12
    assert "not bids" in pressure["note"]


def test_evidence_gate_only_opens_after_four_independent_checks() -> None:
    current = {
        "relative_volume": 3.0,
        "recent_relative_volume": 4.0,
        "momentum_15m_pct": 4.0,
        "momentum_acceleration_pct": 1.0,
        "vwap_position_pct": 1.0,
        "breakout_pct": 1.0,
    }

    gate = _evidence_gate(current, [])

    assert gate["state"] == "ready"
    assert gate["threshold"] == 4
    assert gate["count"] >= gate["threshold"]


def test_ticker_detail_prefers_market_state_and_uses_scan_outcome(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "detail-market.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    insert_filing("detail-filing", "PEN", 1.7, 88, captured_at, "P")
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "detail-snapshot", "PEN", 31, "EARLY", "regular", 1.9, 9.0, 1.1,
                3.3, 3.0, 4.0, 0.8, 900_000, captured_at,
                '["Fresh volume"]', '["Low float risk"]', captured_at,
            ),
        )
        database.execute(
            """
            INSERT INTO scan_outcomes(
                snapshot_id,ticker,base_price,base_at,return_1h_pct,updated_at
            ) VALUES(?,?,?,?,?,?)
            """,
            ("detail-snapshot", "PEN", 1.9, captured_at, 6.2, captured_at),
        )

    detail = ticker_detail_data("PEN")

    assert detail is not None
    assert detail["current"]["source"] == "market"
    assert detail["current"]["signals"] == ["Fresh volume"]
    assert detail["current"]["return_1h_pct"] == 6.2
    assert detail["events"][0]["evidence_label"] == "Verified insider purchase"


def test_radar_marks_new_state_seen(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "radar.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("user", "watcher", "Watcher", "active", captured_at),
        )
        database.execute(
            "INSERT INTO watches(user_id,ticker,created_at,last_seen_at) VALUES(?,?,?,NULL)",
            ("user", "RAD", captured_at),
        )
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "radar-snapshot", "RAD", 22, "EARLY", "regular", 2.1, 7.0, 1.0,
                2.0, 2.5, 3.0, 0.5, 500_000, captured_at, "[]", "[]", captured_at,
            ),
        )

    first = radar_data("user", mark_seen=True)
    second = radar_data("user")

    assert first[0]["has_update"] is True
    assert first[0]["source"] == "market"
    assert first[0]["price"] == 2.1
    assert first[0]["evidence_gate"]["count"] >= 2
    assert second[0]["has_update"] is False


def test_radar_uses_filing_price_when_a_market_snapshot_is_missing(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "radar-filing.db")
    init_db()
    filed_at = datetime.now(UTC).isoformat()
    insert_filing("radar-filing", "FILE", 0.72, 73, filed_at, "P")
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("user", "watcher", "Watcher", "active", filed_at),
        )
        database.execute(
            "INSERT INTO watches(user_id,ticker,created_at,last_seen_at) VALUES(?,?,?,NULL)",
            ("user", "FILE", filed_at),
        )

    result = radar_data("user")

    assert result[0]["source"] == "sec"
    assert result[0]["price"] == 0.72
    assert result[0]["change_pct"] == 6.5
    assert result[0]["evidence_gate"]["checks"] == ["Positive SEC catalyst"]
