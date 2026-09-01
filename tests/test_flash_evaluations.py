from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from runner_watch.source_catalog import SourcePolicy
from runner_web import db
from runner_web.ai_kol import FLASH, flash_version_snapshot
from runner_web.db import connection, init_db
from runner_web.flash_evaluations import (
    correct_flash_outcome,
    flash_record,
    forecast_for_report,
    prepare_forecast_evidence,
    record_flash_forecast,
    refresh_flash_forecasts,
    validate_forecast,
)
from runner_web.ingestion import register_source


@pytest.fixture
def flash_db(tmp_path: Path, monkeypatch: MonkeyPatch) -> Path:
    path = tmp_path / "flash-evaluations.db"
    monkeypatch.setattr(db, "DATABASE_PATH", path)
    init_db()
    timestamp = "2026-08-24T19:00:00+00:00"
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("flash-user", "flash_user", "Flash user", "active", timestamp),
        )
    return path


def _report(
    report_id: str,
    ticker: str,
    *,
    start_price: float = 100.0,
    start_at: str = "2026-08-24T19:00:00+00:00",
    visibility: str = "public",
) -> dict[str, Any]:
    timestamp = start_at
    evidence = {
        "exchange": "NASDAQ",
        "forecast_start": {
            "eligibility": "eligible",
            "ineligibility_reason": None,
            "price": start_price,
            "at": timestamp,
            "source": "test_archive",
            "market_session": "regular",
        },
    }
    version_id = flash_version_snapshot()["id"]
    with connection() as database:
        database.execute(
            """
            INSERT INTO research_commissions(
                id,public_id,user_id,ticker,evidence_key,status,requested_model,model,
                headline,summary,evidence_snapshot_json,evidence_as_of,visibility,
                created_at,updated_at,completed_at,flash_version_id
            ) VALUES(?,?,?,?,?,'complete',?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                report_id,
                f"public-{report_id}",
                "flash-user",
                ticker,
                f"evidence-{report_id}",
                FLASH.model,
                FLASH.model,
                f"{ticker} report",
                "Frozen test evidence.",
                json.dumps(evidence),
                timestamp,
                visibility,
                timestamp,
                timestamp,
                timestamp,
                version_id,
            ),
        )
        row = database.execute(
            "SELECT * FROM research_commissions WHERE id=?", (report_id,)
        ).fetchone()
    assert row is not None
    return dict(row)


def _record(
    report_id: str,
    ticker: str,
    direction: str,
    probability_up: float,
    *,
    start_at: str = "2026-08-24T19:00:00+00:00",
) -> dict[str, Any]:
    report = _report(report_id, ticker, start_at=start_at)
    with connection() as database:
        return record_flash_forecast(
            database,
            report,
            {
                "forecast": {
                    "direction": direction,
                    "probability_up": probability_up,
                    "reason": "The frozen evidence supports this test call.",
                }
            },
            resolved_model=FLASH.model,
            usage={"generation": {"provider_request_id": f"request-{report_id}"}},
            at=start_at,
        )


def _bar(
    ticker: str,
    close: float,
    *,
    timestamp: str = "2026-08-25T19:55:00+00:00",
) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,open,high,low,close,volume,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "test_archive",
                ticker,
                "5m",
                timestamp,
                100.0,
                max(100.0, close),
                min(100.0, close),
                close,
                1000,
                timestamp,
                timestamp,
            ),
        )


def test_fixed_contract_scores_hits_misses_and_no_calls(flash_db: Path) -> None:
    del flash_db
    _record("hit-report", "HIT", "up", 0.6)
    _record("miss-report", "MISS", "down", 0.4)
    no_call = _record("no-call-report", "WAIT", "no_call", 0.5)
    _bar("HIT", 101.0)
    _bar("MISS", 101.0)

    result = refresh_flash_forecasts(
        datetime(2026, 8, 25, 21, 0, tzinfo=UTC), fetch_market_data=False
    )
    hit = forecast_for_report("hit-report")
    miss = forecast_for_report("miss-report")
    record = flash_record()

    assert result == {
        "pending": 2,
        "checked": 2,
        "resolved": 2,
        "voided": 0,
        "reviewed": 0,
    }
    assert hit is not None and hit["classification"] == "hit"
    assert hit["return_pct"] == 1.0
    assert miss is not None and miss["classification"] == "miss"
    assert miss["miss_reason"] == "wrong_way"
    assert no_call["status"] == "no_call"
    assert record["current_version"]["hits"] == 1
    assert record["current_version"]["misses"] == 1
    assert record["current_version"]["no_calls"] == 1
    assert record["current_version"]["hit_rate"] == 0.5
    assert record["current_version"]["headline_rate_visible"] is False
    assert record["current_version"]["forecast_coverage"] == 0.6667
    assert record["current_version"]["brier_score"] == 0.26
    assert record["current_version"]["median_signed_move_pct"] == 0.0
    assert len(record["recent_results"]) == 3
    assert refresh_flash_forecasts(
        datetime(2026, 8, 25, 22, 0, tzinfo=UTC), fetch_market_data=False
    )["resolved"] == 0


def test_half_percent_boundary_is_not_a_hit(flash_db: Path) -> None:
    del flash_db
    _record("boundary-report", "EDGE", "up", 0.6)
    _bar("EDGE", 100.5)

    refresh_flash_forecasts(
        datetime(2026, 8, 25, 21, 0, tzinfo=UTC), fetch_market_data=False
    )
    outcome = forecast_for_report("boundary-report")

    assert outcome is not None
    assert outcome["classification"] == "miss"
    assert outcome["miss_reason"] == "no_meaningful_move"


def test_missing_start_price_is_visible_but_never_scored(flash_db: Path) -> None:
    del flash_db
    report = _report("unscored-report", "NONE")
    evidence = json.loads(report["evidence_snapshot_json"])
    evidence["forecast_start"] = {
        "eligibility": "unscored",
        "ineligibility_reason": "No fresh regular-hours price was available.",
        "price": None,
        "at": None,
        "source": None,
        "market_session": "regular",
    }
    report["evidence_snapshot_json"] = json.dumps(evidence)
    with connection() as database:
        outcome = record_flash_forecast(
            database,
            report,
            {
                "forecast": {
                    "direction": "up",
                    "probability_up": 0.7,
                    "reason": "A directional view without a reliable price anchor.",
                }
            },
            resolved_model=FLASH.model,
            usage={},
        )

    assert outcome["status"] == "void"
    assert flash_record()["current_version"]["settled"] == 0
    assert flash_record()["current_version"]["voids"] == 1


def test_reverse_split_voids_the_result(flash_db: Path) -> None:
    del flash_db
    _record("split-report", "SPLT", "up", 0.7)
    _bar("SPLT", 505.0)
    register_source(
        SourcePolicy(
            source="test",
            feed="actions",
            title="Test corporate actions",
            owner="Test",
            terms_url="https://example.test/terms",
            credential_env=None,
            expected_cadence_seconds=None,
            stale_after_seconds=None,
            schedule="event",
            storage_policy="normalized_only",
            display_policy="source_link_with_attribution",
            attribution="Test",
        )
    )
    with connection() as database:
        database.execute(
            """
            INSERT INTO ingestion_runs(
                id,source,feed,locator,status,started_at,finished_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "split-run",
                "test",
                "actions",
                "https://example.test/actions",
                "success",
                "2026-08-25T14:00:00+00:00",
                "2026-08-25T14:00:00+00:00",
            ),
        )
        database.execute(
            """
            INSERT INTO market_events(
                source,feed,event_id,version,ticker,event_type,event_at,status,
                source_url,payload_json,first_run_id,last_run_id,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "test",
                "actions",
                "split-event",
                "v1",
                "SPLT",
                "reverse_split",
                "2026-08-25T14:00:00+00:00",
                "active",
                "https://example.test/actions/split",
                '{"ratio":"1:5"}',
                "split-run",
                "split-run",
                "2026-08-25T14:00:00+00:00",
                "2026-08-25T14:00:00+00:00",
            ),
        )

    result = refresh_flash_forecasts(
        datetime(2026, 8, 25, 21, 0, tzinfo=UTC), fetch_market_data=False
    )
    outcome = forecast_for_report("split-report")

    assert result["voided"] == 1
    assert outcome is not None and outcome["status"] == "void"
    assert outcome["void_reason"] == "A reverse split changed the price basis."
    assert flash_record()["current_version"]["settled"] == 0


def test_weekend_forecast_uses_monday_session(flash_db: Path) -> None:
    del flash_db
    friday = "2026-08-28T19:00:00+00:00"
    _record("weekend-report", "WKND", "down", 0.4, start_at=friday)
    _bar("WKND", 98.5, timestamp="2026-08-31T19:55:00+00:00")

    result = refresh_flash_forecasts(
        datetime(2026, 8, 31, 21, 0, tzinfo=UTC), fetch_market_data=False
    )
    outcome = forecast_for_report("weekend-report")

    assert result["resolved"] == 1
    assert outcome is not None and outcome["classification"] == "hit"
    assert outcome["target_session_date"] == "2026-08-31"


def test_correction_keeps_the_previous_result_in_history(flash_db: Path) -> None:
    del flash_db
    created = _record("corrected-report", "FIX", "up", 0.6)
    _bar("FIX", 101.0)
    refresh_flash_forecasts(
        datetime(2026, 8, 25, 21, 0, tzinfo=UTC), fetch_market_data=False
    )

    corrected = correct_flash_outcome(
        created["id"],
        reason="The first closing bar was wrong.",
        end_price=99.0,
        observed_at="2026-08-25T19:55:00+00:00",
        at=datetime(2026, 8, 26, 16, 0, tzinfo=UTC),
    )
    outcome = forecast_for_report("corrected-report")

    assert corrected["classification"] == "miss"
    assert outcome is not None and outcome["classification"] == "miss"
    assert outcome["correction_count"] == 1
    assert outcome["corrections"][0]["reason"] == "The first closing bar was wrong."
    assert outcome["corrections"][0]["previous"]["classification"] == "hit"
    with connection() as database:
        events = database.execute(
            """
            SELECT event_type FROM flash_evaluation_events
            WHERE forecast_id=? ORDER BY created_at
            """,
            (created["id"],),
        ).fetchall()
    assert [row["event_type"] for row in events] == ["created", "resolved", "corrected"]


def test_prepare_forecast_evidence_freezes_only_fresh_prices(flash_db: Path) -> None:
    del flash_db
    fresh = prepare_forecast_evidence(
        "FRESH",
        {"price": 12.5, "captured_at": "2026-08-24T18:58:00+00:00"},
        evidence_as_of="2026-08-24T19:00:00+00:00",
    )
    stale = prepare_forecast_evidence(
        "STALE",
        {"price": 12.5, "captured_at": "2026-08-24T18:00:00+00:00"},
        evidence_as_of="2026-08-24T19:00:00+00:00",
    )

    assert fresh["forecast_start"]["eligibility"] == "eligible"
    assert fresh["forecast_start"]["price"] == 12.5
    assert stale["forecast_start"]["eligibility"] == "unscored"
    assert stale["forecast_start"]["price"] is None


@pytest.mark.parametrize(
    ("forecast", "message"),
    [
        ({"direction": "up", "probability_up": 0.54, "reason": "Weak."}, "below 55%"),
        ({"direction": "down", "probability_up": 0.46, "reason": "Weak."}, "above 45%"),
        (
            {"direction": "no_call", "probability_up": 0.7, "reason": "Conflicting."},
            "outside the uncertainty range",
        ),
    ],
)
def test_forecast_probability_must_match_direction(
    forecast: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_forecast(forecast)
