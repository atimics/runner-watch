import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from starlette.requests import Request

from runner_web import db, operations
from runner_web.data_health import data_health, quote_health, sports_health
from runner_web.db import connection, init_db
from tests.test_ranker import _seed_ranker_data

NOW = datetime(2026, 9, 4, 18, 56, tzinfo=UTC)


def test_market_health_checks_quotes_from_the_latest_scan(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "quotes.db")
    init_db()
    _seed_ranker_data(group_count=2)
    with connection() as database:
        database.execute(
            "UPDATE scan_snapshots SET quote_time=? WHERE scan_run_id='run-0'",
            (NOW.isoformat(),),
        )
    assert data_health("runners", at=NOW)["feed"]["status"] == "stale"
    with connection() as database:
        database.execute(
            "UPDATE scan_snapshots SET quote_time=?,captured_at=? WHERE scan_run_id='run-1'",
            (NOW.isoformat(), NOW.isoformat()),
        )
    assert data_health("runners", at=NOW)["status"] == "ok"


def test_stale_quotes_fail_even_when_collection_finished_recently() -> None:
    result = quote_health(["2026-09-04T12:05:00-04:00"], NOW)
    assert result["status"] == "stale"
    assert result["age_seconds"] == 171 * 60


def test_fresh_quotes_use_actual_instants_across_timezones() -> None:
    result = quote_health(["2026-09-04T14:45:00-04:00", "2026-09-04T18:42:00Z"], NOW)
    assert result["status"] == "ok"
    assert result["age_seconds"] == 11 * 60


@pytest.mark.parametrize("quote", [None, "broken", "2027-01-01T00:00:00Z"])
def test_invalid_quotes_cannot_make_the_feed_healthy(quote) -> None:
    assert quote_health([quote], NOW)["status"] == "missing"


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-05T18:00:00Z",
        "2026-09-07T18:00:00Z",
        "2026-11-27T18:01:00Z",
        "2026-09-04T21:00:00Z",
        "2026-09-04T12:00:00Z",
    ],
)
def test_closed_sessions_allow_idle_market_data(timestamp: str) -> None:
    assert quote_health([], datetime.fromisoformat(timestamp))["status"] == "idle"


def test_opening_grace_tracks_daylight_saving_time() -> None:
    assert quote_health([], datetime(2026, 11, 30, 14, 40, tzinfo=UTC))["status"] == "warming"
    assert quote_health([], datetime(2026, 11, 30, 15, 30, tzinfo=UTC))["status"] == "missing"


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        (None, "missing"),
        ({"status": "success", "finished_at": NOW.isoformat(), "received_count": 5}, "ok"),
        ({"status": "error", "finished_at": NOW.isoformat(), "received_count": 0}, "error"),
        ({"status": "success", "finished_at": "2026-09-04T17:00:00Z"}, "stale"),
        ({"status": "success", "finished_at": "2027-01-01T00:00:00Z"}, "invalid"),
        ({"status": "success", "finished_at": NOW.isoformat(), "received_count": 0}, "ok"),
    ],
)
def test_sports_requires_recent_successful_data(run, expected: str) -> None:
    assert sports_health(run, NOW)["status"] == expected


def test_database_failure_is_reported_without_error_details(monkeypatch) -> None:
    def unavailable():
        raise RuntimeError("private connection details")

    monkeypatch.setattr("runner_web.data_health.connection", unavailable)
    assert data_health("sports", at=NOW)["feed"] == {"status": "unavailable"}


def test_latest_failed_sports_run_is_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "feed.db")
    init_db()
    with connection() as database:
        for index, status in enumerate(("success", "error")):
            timestamp = (NOW - timedelta(minutes=10 - index)).isoformat()
            database.execute(
                """
                INSERT INTO ingestion_runs(
                    id,source,feed,locator,status,received_count,started_at,finished_at,error
                ) VALUES(?, 'espn', 'sports_scoreboard_preview', 'https://example.test',
                         ?, 5, ?, ?, 'private provider error')
                """,
                (str(index), status, timestamp, timestamp),
            )
    result = data_health("sports", at=NOW)
    assert result["status"] == "degraded"
    assert result["feed"]["status"] == "error"
    assert "private" not in json.dumps(result)


def test_public_data_health_checks_the_requested_product(monkeypatch) -> None:
    seen = []

    def health(product):
        seen.append(product)
        return {"status": "degraded", "feed": {"status": "stale", "private": "details"}}

    monkeypatch.setattr(operations, "data_health", health)
    for host in ("sports.rati.chat", "runners.rati.chat"):
        request = Request(
            {"type": "http", "path": "/health/data", "headers": [(b"host", host.encode())]}
        )
        response = operations.data_health_api(request)
        assert response.status_code == 503
        assert json.loads(response.body) == {"status": "degraded"}
    assert seen == ["sports", "runners"]


def test_one_recent_quote_leaves_poor_board_coverage_visible():
    result = quote_health([NOW] + [NOW - timedelta(hours=2)] * 9, NOW)
    assert result["status"] == "stale"
    assert result["age_seconds"] == 0
    assert result["fresh_quote_count"] == result["usable_count"] == 1
    assert result["quote_count"] == 10
    assert result["usable_fraction"] == 0.1


def test_coverage_includes_missing_and_invalid_quote_clocks():
    result = quote_health([NOW] * 3 + [None, "broken"], NOW)
    assert result["status"] == "stale"
    assert result["invalid_quote_count"] == 2
    assert result["usable_fraction"] == 0.6
    assert quote_health([NOW] * 4 + [None], NOW)["status"] == "ok"


def test_recent_prices_preserve_the_age_of_the_scan_assessment():
    result = quote_health([NOW] * 5, NOW, assessment_times=[NOW - timedelta(hours=2)] * 5)
    assert result["status"] == "stale"
    assert result["fresh_quote_count"] == 5
    assert result["fresh_assessment_count"] == result["usable_count"] == 0


@pytest.mark.parametrize("collected_offset", [0, 1])
def test_health_uses_the_same_observed_quote_marks_as_the_board(
    tmp_path, monkeypatch, collected_offset
):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "marks.db")
    init_db()
    _seed_ranker_data(group_count=1)
    with connection() as database:
        database.execute("UPDATE scan_snapshots SET captured_at=?", (NOW.isoformat(),))
        database.execute(
            """INSERT INTO ticker_quotes(
                ticker,price,change_pct,observed_at,collected_at,status,requested_at,source
            ) SELECT DISTINCT ticker,1,0,?,?,'ok',?,'yahoo' FROM scan_snapshots""",
            (
                NOW.isoformat(),
                (NOW + timedelta(minutes=collected_offset)).isoformat(),
                NOW.isoformat(),
            ),
        )
    result = data_health("runners", at=NOW)
    assert result["status"] == ("ok" if collected_offset == 0 else "degraded")
    assert result["feed"]["usable_count"] == (4 if collected_offset == 0 else 0)
