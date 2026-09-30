from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.market_reports import market_report, refresh_market_reports
from runner_web.report_sources import (
    decode_sources,
    encode_sources,
    report_sources,
    version_token,
)
from tests.test_market_reports import _insert_scan_run, _insert_snapshot


def test_sources_list_the_scan_runs_a_payload_used() -> None:
    sources = report_sources(
        {"source_scan_run_id": "b", "comparison_scan_run_id": "a", "as_of": "t2"}, "t1"
    )

    assert [(item["role"], item["ref"], item["as_of"]) for item in sources] == [
        ("source", "b", "t2"),
        ("comparison", "a", "t1"),
    ]
    assert report_sources({"source_scan_run_id": "b", "comparison_scan_run_id": "b"}) == [
        sources[0] | {"as_of": None}
    ]
    assert report_sources({}) == []


def test_version_token_follows_the_inputs_only() -> None:
    one = report_sources({"source_scan_run_id": "b", "as_of": "t2"})
    same_inputs_later = report_sources({"source_scan_run_id": "b", "as_of": "t3"})
    other = report_sources({"source_scan_run_id": "c"})

    assert version_token(one) == version_token(same_inputs_later)
    assert version_token(one) != version_token(other)


def test_decode_tolerates_missing_or_damaged_records() -> None:
    assert decode_sources(None) == []
    assert decode_sources("not json") == []
    assert decode_sources('{"a":1}') == []
    assert decode_sources(encode_sources([{"ref": "x"}, "bad"])) == [{"ref": "x"}]  # type: ignore[list-item]


def test_a_new_report_stores_sources_and_old_reports_read_as_empty(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "sources.db")
    init_db()
    captured = "2026-08-24T12:55:00+00:00"
    _insert_scan_run("run-1", captured, 1)
    _insert_snapshot(
        "snap-1", "run-1", "ONE", 75, 1, captured, session="pre", price=1.5, change_pct=12.0
    )

    refresh_market_reports(datetime(2026, 8, 24, 13, 5, tzinfo=UTC))
    report = market_report("2026-08-24", "pre_market")

    assert report is not None
    assert [item["ref"] for item in report["sources"]] == ["run-1"]
    assert report["version_token"] == version_token(report["sources"])

    with connection() as database:
        database.execute("UPDATE market_session_reports SET sources_json=NULL,version_token=NULL")
    legacy = market_report("2026-08-24", "pre_market")
    assert legacy is not None
    assert legacy["sources"] == []
    assert legacy["version_token"] is None
