"""Deterministic parity and work-budget tests, not machine-speed assertions."""

from __future__ import annotations

import random
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import pytest

from runner_web import db, main, outcomes
from runner_web.database import DatabaseConnection, postgres_statement
from runner_web.db import connection, init_db
from runner_web.outcome_bars import IndexedBars
from tests.test_mobile import insert_filing, insert_scan_run, insert_scored_snapshot
from tests.test_outcomes import _seed_scan_snapshot
from tests.test_replay import _approve_test_source, _insert_comment, _insert_event


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "performance.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    init_db()


def test_sql_translation_cache_is_bounded_and_semantically_identical():
    postgres_statement.cache_clear()
    templates = [
        "SELECT '?' AS literal, ? AS bound, :named AS named",
        "UPDATE t SET x=MAX(x,?), y=MIN(y,?) WHERE id=?",
        "INSERT OR IGNORE INTO t(a) VALUES(?);",
        "SELECT 'it''s ? :not_bound', x::text FROM t WHERE x=:value",
    ]
    expected = [postgres_statement.__wrapped__(sql) for sql in templates]
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(postgres_statement, templates * 20)) == expected * 20
    previous_hits = postgres_statement.cache_info().hits
    assert [postgres_statement(sql) for sql in templates] == expected
    assert postgres_statement.cache_info().hits == previous_hits + len(templates)
    for index in range(600):
        postgres_statement(f"SELECT ? AS column_{index}")
    assert postgres_statement.cache_info().currsize == 512
    assert [postgres_statement(sql) for sql in templates] == expected
    postgres_statement.cache_clear()


def test_cached_sql_does_not_cache_execution_or_parameter_values():
    calls = []

    class Raw:
        def execute(self, sql, params):
            calls.append((sql, params))
            return type("Cursor", (), {"description": ()})()

    handle = DatabaseConnection(Raw(), "postgres")
    for value in [1, 2, None]:
        handle.execute("SELECT ?", (value,))
    assert calls == [("SELECT %s", (value,)) for value in [1, 2, None]]


@pytest.mark.parametrize("month,day", [(3, 7), (9, 21), (10, 31)])
def test_indexed_history_matches_legacy_labels_and_session_closes(month, day):
    # Include both US DST transitions, gaps and duplicate instants.
    rng = random.Random(19)
    start = datetime(2026, month, day, tzinfo=UTC)
    bars = []
    for index in range(2200):
        if rng.random() < 0.07:
            continue
        close = rng.uniform(94, 110)
        bar = (start + timedelta(minutes=5 * index), close + 1, close - 1, close)
        bars.append(bar)
        if index % 71 == 0:
            bars.append((bar[0], close + 2, close - 2, close + 0.5))
    indexed = IndexedBars(bars)
    assert tuple(indexed) == tuple(bars)
    assert indexed[:2] == tuple(bars[:2])
    moments = [start - timedelta(minutes=1), start + timedelta(days=12)]
    moments += [start + timedelta(minutes=rng.randrange(11_000)) for _ in range(40)]
    for base in moments:
        assert outcomes._first_price_at_or_after(
            indexed, base
        ) == outcomes._first_price_at_or_after(bars, base)
        assert outcomes._price_near_target(indexed, base) == outcomes._price_near_target(bars, base)
        assert outcomes.barrier_outcome(indexed, base, 100) == outcomes.barrier_outcome(
            bars, base, 100
        )
        for horizon in ("1h", "1d", "5d"):
            assert outcomes._scan_horizon_price(
                indexed, base, horizon
            ) == outcomes._scan_horizon_price(bars, base, horizon)
        assert outcomes.case_horizon_outcome(
            indexed, base, 100, 60, at=base + timedelta(days=2)
        ) == outcomes.case_horizon_outcome(bars, base, 100, 60, at=base + timedelta(days=2))


def test_index_boundaries_ambiguity_gaps_and_input_ownership():
    base = datetime(2026, 9, 21, 15, tzinfo=UTC)
    bars = [(base + timedelta(minutes=i), 109, 95, 100) for i in (0, 5, 60, 70)]
    indexed = IndexedBars(reversed(bars))
    assert indexed.between(base, base + timedelta(minutes=60)) == bars[1:3]
    for target in [base + timedelta(minutes=i) for i in (-10, 0, 15, 50, 60, 80, 81)]:
        assert indexed.near(target, outcomes.BAR_TOLERANCE) == outcomes._price_near_target(
            bars, target
        )
    assert outcomes.barrier_outcome(indexed, base, 100)["barrier_resolution"] == "ambiguous"
    original_count = len(indexed)
    bars.clear()
    assert len(indexed) == original_count
    assert list(IndexedBars([])) == []
    assert IndexedBars([]).near(base, outcomes.BAR_TOLERANCE) is None
    assert IndexedBars([]).session_close(base.date(), 0) is None


def _queue_database():
    raw = sqlite3.connect(":memory:")
    handle = DatabaseConnection(raw, "sqlite")
    handle.execute(
        """CREATE TABLE scan_outcomes (
            snapshot_id TEXT PRIMARY KEY,ticker TEXT,base_at TEXT,next_attempt_at TEXT,
            barrier_label TEXT,return_60m_pct REAL,return_1h_pct REAL,return_1d_pct REAL,
            return_5d_pct REAL,payload TEXT
        )"""
    )
    handle.execute("CREATE INDEX scan_outcomes_base_at ON scan_outcomes(base_at DESC)")
    return handle


def _legacy_pending(database, current, cutoff, limit):
    rows = database.execute(
        "SELECT * FROM scan_outcomes WHERE base_at>=? "
        "AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
        (cutoff, current.isoformat()),
    ).fetchall()
    selected = []
    for raw in rows:
        row = dict(raw)
        horizon = outcomes.due_horizons(row, current)
        mature = current - outcomes._parsed_moment(row["base_at"]) >= outcomes.BARRIER_HORIZON
        barrier = mature and row["barrier_label"] is None
        terminal = mature and row["return_60m_pct"] is None
        if horizon or barrier or terminal:
            selected.append((row, horizon, barrier, terminal))
    selected.sort(key=lambda item: (item[0]["base_at"], item[0]["snapshot_id"]))
    return selected[:limit], len(rows)


def test_queue_fetch_budget_does_not_scale_with_resolved_history():
    database = _queue_database()
    try:
        now = datetime(2026, 9, 21, 15, tzinfo=UTC)
        stamp = (now - timedelta(days=6)).isoformat()
        database.executemany(
            "INSERT INTO scan_outcomes VALUES(?,?,?,NULL,?,?,?,?,?,?)",
            [(f"s{i:05}", "A", stamp, "up", 1, 1, 1, None, "x" * 300) for i in range(20_000)],
        )
        cutoff = (now - timedelta(days=10)).isoformat()
        pending, count, examined = outcomes._pending_scan_outcomes(database, now, cutoff, limit=25)
        expected, expected_count = _legacy_pending(database, now, cutoff, 25)
        assert pending == expected
        assert count == expected_count == 20_000
        assert examined == 25
    finally:
        database.raw.close()


def test_keyset_queue_crosses_non_due_pages_and_handles_offsets_and_retries():
    database = _queue_database()
    try:
        now = datetime(2026, 9, 21, 15, tzinfo=UTC)
        records = []
        for index in range(12):
            # Lexically earlier than the due rows but only waiting for the 5d outcome.
            records.append(
                (
                    f"wait-{index}",
                    "A",
                    (now - timedelta(days=2)).isoformat(),
                    None,
                    "up",
                    1,
                    1,
                    1,
                    None,
                    "",
                )
            )
        for index in range(8):
            stamp = (now - timedelta(hours=2)).astimezone(timezone(timedelta(hours=-4))).isoformat()
            records.append((f"due-{index}", "A", stamp, None, "up", None, 1, 1, 1, ""))
        records.extend(
            [
                (
                    "retry",
                    "A",
                    (now - timedelta(days=7)).isoformat(),
                    (now + timedelta(hours=1)).isoformat(),
                    None,
                    None,
                    None,
                    None,
                    None,
                    "",
                ),
                (
                    "resolved",
                    "A",
                    (now - timedelta(days=8)).isoformat(),
                    None,
                    "up",
                    0,
                    0,
                    0,
                    0,
                    "",
                ),
            ]
        )
        database.executemany("INSERT INTO scan_outcomes VALUES(?,?,?,?,?,?,?,?,?,?)", records)
        cutoff = (now - timedelta(days=10)).isoformat()
        pending, count, examined = outcomes._pending_scan_outcomes(database, now, cutoff, limit=3)
        expected, expected_count = _legacy_pending(database, now, cutoff, 3)
        assert pending == expected
        assert count == expected_count
        assert [item[0]["snapshot_id"] for item in pending] == ["due-0", "due-1", "due-2"]
        assert examined == 15
        with pytest.raises(ValueError, match="positive"):
            outcomes._pending_scan_outcomes(database, now, cutoff, limit=0)
    finally:
        database.raw.close()


def test_outcome_refresh_does_not_rewrite_correct_base_timestamps(isolated_database):
    now = datetime(2026, 9, 21, 15, tzinfo=UTC)
    base = now - timedelta(hours=2)
    with connection() as handle:
        _seed_scan_snapshot(handle, "correct", "A", base)
        _seed_scan_snapshot(handle, "incorrect", "B", base)
        handle.execute(
            "INSERT INTO scan_outcomes(snapshot_id,ticker,base_price,base_at,updated_at) "
            "VALUES('incorrect','B',2,?,?)",
            ((base - timedelta(minutes=1)).isoformat(), now.isoformat()),
        )
        handle.executescript(
            """CREATE TABLE timestamp_writes(snapshot_id TEXT);
            CREATE TRIGGER record_timestamp_write AFTER UPDATE OF base_at ON scan_outcomes
            BEGIN INSERT INTO timestamp_writes VALUES(NEW.snapshot_id); END;"""
        )
    outcomes.refresh_scan_outcomes(now)
    outcomes.refresh_scan_outcomes(now + timedelta(minutes=1))
    with connection() as handle:
        assert [row[0] for row in handle.execute("SELECT * FROM timestamp_writes")] == ["incorrect"]


def test_pulse_scopes_context_but_keeps_off_board_ticker_detail(isolated_database):
    now = datetime(2026, 9, 21, 15, tzinfo=UTC)
    stamp = (now - timedelta(minutes=1)).isoformat()
    insert_scan_run("board", stamp, 1)
    insert_scored_snapshot("snapshot", "board", "ONE", 50, 1, stamp)
    insert_filing("best", "ONE", 2, 80, stamp)
    insert_filing("second", "ONE", 2, 40, stamp)
    insert_filing("outside", "OTHER", 2, 100, stamp)
    insert_filing("future", "ONE", 2, 100, (now + timedelta(minutes=1)).isoformat())
    _insert_comment("first-comment", now - timedelta(minutes=2))
    _insert_comment("other-comment", now - timedelta(minutes=2))
    _insert_event("first-event", now - timedelta(minutes=2))
    _insert_event("other-event", now - timedelta(minutes=2))
    _approve_test_source("test_discovery", "mixed")
    with connection() as handle:
        handle.execute("UPDATE ticker_comments SET ticker='OTHER' WHERE id='other-comment'")
        handle.execute("UPDATE market_events SET ticker='OTHER' WHERE event_id='other-event'")
    board = main._pulse_scoring_inputs(at=now)
    assert set(board["filings_by_ticker"]) == {"ONE"}
    assert board["filing_counts"] == {"ONE": 2}
    assert board["filings_by_ticker"]["ONE"]["accession"] == "best"
    assert "matching_filing_count" not in board["filings_by_ticker"]["ONE"]
    assert set(board["community"]) == {"ONE"}
    assert set(board["market_events_by_ticker"]) == {"ONE"}
    outside = main._pulse_scoring_inputs(at=now, ticker="OTHER")
    assert not outside["market_rows"]
    assert outside["filing_counts"] == {"OTHER": 1}
    assert set(outside["community"]) == {"OTHER"}
    assert set(outside["market_events_by_ticker"]) == {"OTHER"}


def test_empty_board_does_not_materialize_global_filings(isolated_database):
    now = datetime(2026, 9, 21, 15, tzinfo=UTC)
    insert_filing("outside", "OTHER", 2, 100, now.isoformat())
    board = main._pulse_scoring_inputs(at=now)
    assert board["filing_counts"] == board["filings_by_ticker"] == {}
    assert main._pulse_scoring_inputs(at=now, ticker="OTHER")["filing_counts"] == {"OTHER": 1}
