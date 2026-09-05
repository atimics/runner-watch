from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch
from starlette.requests import Request

from runner_web import db
from runner_web.ai_kol import FLASH, flash_version_snapshot
from runner_web.db import connection, init_db
from runner_web.flash_evaluations import (
    forecast_for_report,
    record_flash_forecast,
    refresh_flash_forecasts,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_ID = "correction-report"
OBSERVED_AT = "2026-08-25T19:55:00+00:00"


@pytest.fixture
def scored_forecast(tmp_path: Path, monkeypatch: MonkeyPatch) -> tuple[Path, str]:
    path = tmp_path / "flash-correction.db"
    monkeypatch.setattr(db, "DATABASE_PATH", path)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    init_db()
    timestamp = "2026-08-24T19:00:00+00:00"
    evidence = {
        "exchange": "NASDAQ",
        "forecast_start": {
            "eligibility": "eligible",
            "price": 100.0,
            "at": timestamp,
            "source": "test_archive",
            "market_session": "regular",
        },
    }
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("flash-user", "flash_user", "Flash user", "active", timestamp),
        )
        database.execute(
            """
            INSERT INTO research_commissions(
                id,public_id,user_id,ticker,evidence_key,status,requested_model,model,
                headline,summary,evidence_snapshot_json,evidence_as_of,visibility,
                created_at,updated_at,completed_at,flash_version_id,usage_json
            ) VALUES(?,?,?,?,?,'complete',?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                REPORT_ID,
                "public-correction",
                "flash-user",
                "FIX",
                "test-evidence",
                FLASH.model,
                FLASH.model,
                "FIX report",
                "Frozen evidence.",
                json.dumps(evidence),
                timestamp,
                "public",
                timestamp,
                timestamp,
                timestamp,
                flash_version_snapshot()["id"],
                json.dumps({"context": {}}),
            ),
        )
        report = database.execute(
            "SELECT * FROM research_commissions WHERE id=?", (REPORT_ID,)
        ).fetchone()
        created = record_flash_forecast(
            database,
            dict(report),
            {"forecast": {"direction": "up", "probability_up": 0.6, "reason": "Test call."}},
            resolved_model=FLASH.model,
            usage={},
            at=timestamp,
        )
        database.execute(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,open,high,low,close,volume,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "test_archive",
                "FIX",
                "5m",
                OBSERVED_AT,
                100.0,
                101.0,
                100.0,
                101.0,
                1000,
                OBSERVED_AT,
                OBSERVED_AT,
            ),
        )
    result = refresh_flash_forecasts(
        datetime(2026, 8, 25, 21, 0, tzinfo=UTC), fetch_market_data=False
    )
    assert result["resolved"] == 1
    return path, str(created["id"])


def _run(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "DATABASE_PATH": str(path),
        "DATABASE_URL": "",
        "REQUIRE_DATABASE_URL": "0",
        "REQUIRE_DATABASE_TLS": "0",
    }
    return subprocess.run(
        [sys.executable, "-m", "runner_web.flash_correction", *args],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _saved_state() -> dict[str, Any]:
    with connection() as database:
        return {
            table: [dict(row) for row in database.execute(f"SELECT * FROM {table}").fetchall()]
            for table in ("flash_forecasts", "flash_forecast_outcomes", "flash_evaluation_events")
        }


def test_cli_corrects_result_keeps_history_and_renders_public_reason(
    scored_forecast: tuple[Path, str],
) -> None:
    from runner_web import main as web_main

    path, forecast_id = scored_forecast
    reason = "The provider corrected <closing> prices & volume."
    result = _run(
        path,
        forecast_id,
        "--end-price",
        "99",
        "--observed-at",
        OBSERVED_AT,
        "--reason",
        reason,
    )

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["forecast_id"] == forecast_id
    assert receipt["classification"] == "miss"
    assert receipt["return_pct"] == -1.0
    assert receipt["correction_reason"] == reason
    outcome = forecast_for_report(REPORT_ID)
    assert outcome is not None
    assert outcome["correction_count"] == 1
    assert outcome["corrections"][0]["previous"]["classification"] == "hit"
    assert outcome["corrections"][0]["current"]["classification"] == "miss"
    assert outcome["corrections"][0]["reason"] == reason
    request = Request({"type": "http", "method": "GET", "path": "/research", "headers": []})
    request.state.csp_nonce = "test"
    page = web_main.research_report_page("public-correction", request, None)
    assert page.status_code == 200
    assert escape(reason) in bytes(page.body).decode()
    assert reason not in bytes(page.body).decode()


def test_cli_void_keeps_previous_scored_result(scored_forecast: tuple[Path, str]) -> None:
    path, forecast_id = scored_forecast
    result = _run(path, forecast_id, "--void", "--reason", "  Price history was revised.  ")

    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "void"
    assert receipt["classification"] is None
    assert receipt["end_price"] is None
    assert receipt["void_reason"] == "Price history was revised."
    outcome = forecast_for_report(REPORT_ID)
    assert outcome is not None
    assert outcome["corrections"][0]["previous"]["end_price"] == 101.0


@pytest.mark.parametrize(
    "args",
    [
        ["--void"],
        ["--void", "--reason", "  "],
        ["--reason", "Fix."],
        ["--end-price", "99", "--reason", "Fix."],
        ["--end-price", "0", "--observed-at", OBSERVED_AT, "--reason", "Fix."],
        ["--end-price", "nan", "--observed-at", OBSERVED_AT, "--reason", "Fix."],
        ["--end-price", "inf", "--observed-at", OBSERVED_AT, "--reason", "Fix."],
        ["--end-price", "99", "--observed-at", "yesterday", "--reason", "Fix."],
        ["--void", "--end-price", "99", "--reason", "Fix."],
        ["--void", "--observed-at", OBSERVED_AT, "--reason", "Fix."],
    ],
)
def test_invalid_input_preserves_outcome_and_history(
    scored_forecast: tuple[Path, str],
    args: list[str],
) -> None:
    path, forecast_id = scored_forecast
    before = _saved_state()

    result = _run(path, forecast_id, *args)

    assert result.returncode == 2
    assert "error:" in result.stderr
    assert result.stdout == ""
    assert _saved_state() == before


def test_unknown_forecast_preserves_history(scored_forecast: tuple[Path, str]) -> None:
    path, _ = scored_forecast
    before = _saved_state()

    result = _run(path, "unknown", "--void", "--reason", "Fix.")

    assert result.returncode == 2
    assert "Flash forecast not found" in result.stderr
    assert _saved_state() == before


def test_help_explains_database_and_public_reason(tmp_path: Path) -> None:
    path = tmp_path / "unused.db"
    result = _run(path, "--help")

    assert result.returncode == 0, result.stderr
    assert "DATABASE_URL" in result.stdout
    assert "DATABASE_PATH" in result.stdout
    assert "reason appears on the report" in result.stdout
    assert not path.exists()
