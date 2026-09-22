#!/usr/bin/env python3
"""Reproduce hot-path comparisons with synthetic data; never connect to production.

Run from a checkout with history and dev dependencies:
  uv run python scripts/benchmark-hot-paths.py --baseline-ref 7e44356

Timing includes index construction. Assertions check results, not elapsed time.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import sqlite3
import statistics
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Force a local-only environment even when invoked from an operations shell.
os.environ["BACKGROUND_WORKERS_ENABLED"] = "0"
os.environ["DATABASE_URL"] = ""
os.environ["REQUIRE_DATABASE_URL"] = "0"
os.environ["TELEGRAM_RUNNER_ALERTS"] = "0"

from runner_web import db, main, outcomes  # noqa: E402
from runner_web.database import DatabaseConnection, postgres_statement  # noqa: E402
from runner_web.outcome_bars import IndexedBars  # noqa: E402


def compare(label, legacy, optimized, repeats, *, normalize=lambda value: value):
    expected, actual = legacy(), optimized()
    assert normalize(expected) == actual, f"{label}: output parity failed"
    old, new = [], []
    # Alternate order to avoid consistently assigning warm caches to one side.
    for iteration in range(repeats):
        functions = [(legacy, old), (optimized, new)]
        if iteration % 2:
            functions.reverse()
        for function, times in functions:
            start = time.perf_counter()
            function()
            times.append((time.perf_counter() - start) * 1000)
    old_median, new_median = statistics.median(old), statistics.median(new)
    return {
        "workload": label,
        "parity": True,
        "legacy_median_ms": round(old_median, 3),
        "optimized_median_ms": round(new_median, 3),
        "speedup": round(old_median / max(new_median, 1e-9), 2),
        "legacy_samples_ms": [round(t, 3) for t in old],
        "optimized_samples_ms": [round(t, 3) for t in new],
    }


def legacy_queue(database, current, cutoff, limit):
    rows = database.execute(
        "SELECT * FROM scan_outcomes WHERE base_at>=? "
        "AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
        (cutoff, current.isoformat()),
    ).fetchall()
    selected = []
    for raw in rows:
        row = dict(raw)
        horizons = outcomes.due_horizons(row, current)
        mature = current - outcomes._parsed_moment(row["base_at"]) >= outcomes.BARRIER_HORIZON
        barrier = mature and row["barrier_label"] is None
        terminal = mature and row["return_60m_pct"] is None
        if horizons or barrier or terminal:
            selected.append((row, horizons, barrier, terminal))
    selected.sort(key=lambda item: (item[0]["base_at"], item[0]["snapshot_id"]))
    return selected[:limit], len(rows)


def baseline_loader(ref):
    # Load only the old pure input-loader definition, not old module startup code.
    source = subprocess.run(
        ["git", "show", f"{ref}:src/runner_web/main.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    tree = ast.parse(source)
    node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_pulse_scoring_inputs"
    )
    namespace = dict(vars(main))
    exec(compile(ast.Module(body=[node], type_ignores=[]), "baseline-loader", "exec"), namespace)
    return namespace["_pulse_scoring_inputs"]


def run(args) -> dict[str, Any]:
    now = datetime(2026, 9, 21, 15, tzinfo=UTC)
    reports = []
    bars = [
        (
            now - timedelta(days=10) + timedelta(minutes=5 * i),
            102 + 8 * math.sin(i / 10),
            98 + 8 * math.sin(i / 10),
            100 + 8 * math.sin(i / 10),
        )
        for i in range(2500)
    ]
    decisions = [bars[100 + i * 80][0] for i in range(20)]

    def price_batch(indexed):
        result = []
        for _ in range(10):
            history = IndexedBars(bars) if indexed else bars
            for base in decisions:
                result.append(
                    (
                        outcomes.barrier_outcome(history, base, 100),
                        outcomes._price_near_target(history, base + timedelta(hours=1)),
                        outcomes._scan_horizon_price(history, base, "1d"),
                        outcomes._scan_horizon_price(history, base, "5d"),
                    )
                )
        return result

    reports.append(
        compare(
            "200 outcomes / 10 tickers / 2500 bars each; includes index build",
            lambda: price_batch(False),
            lambda: price_batch(True),
            args.repeats,
        )
    )
    with tempfile.TemporaryDirectory(prefix="runner-perf-") as tmp:
        # Database globals are explicitly overridden; no caller's database is opened.
        db.DATABASE_URL = ""
        db.REQUIRE_DATABASE_URL = False
        db.DATABASE_PATH = Path(tmp) / "benchmark.db"
        db.init_db()
        with db.connection() as handle:
            stamp = (now - timedelta(minutes=1)).isoformat()
            handle.execute(
                "INSERT INTO scan_runs(id,mode,label,feature_schema_version,requested_symbols,"
                "liquid_symbols,scanned_symbols,candidate_rows,failed_symbols_json,warnings_json,"
                "started_at,finished_at,captured_at) "
                "VALUES('b','penny','Bench','bench',50,50,50,50,"
                "'[]','[]',?,?,?)",
                (stamp, stamp, stamp),
            )
            handle.executemany(
                "INSERT INTO scan_snapshots(id,ticker,score,stage,session,price,change_pct,"
                "momentum_5m_pct,momentum_15m_pct,breakout_pct,dollar_volume,quote_time,"
                "signals_json,risks_json,captured_at,scan_run_id,baseline_rank) "
                "VALUES(?,?,50,'WATCH','REGULAR',2,0,0,0,0,1000000,?,'[]','[]',?,'b',?)",
                [(f"s{i}", f"T{i:04}", stamp, stamp, i + 1) for i in range(50)],
            )
            handle.executemany(
                "INSERT INTO sec_filings(accession,cik,ticker,company,form,kind,sentiment,score,"
                "title,filed_at,filing_url,price,created_at,updated_at,evidence_json) "
                "VALUES(?,1,?,'Bench','4','Ownership update','neutral',?,'Bench',?,"
                "'https://example.test/filing',2,?,?,?)",
                [
                    (f"f{i}", f"T{i % 1500:04}", float(i // 1500), stamp, stamp, stamp, "x" * 1000)
                    for i in range(15_000)
                ],
            )
            handle.execute("ANALYZE")
        old_loader = baseline_loader(args.baseline_ref)
        universe = {f"T{i:04}" for i in range(50)}

        def scoped(value):
            copy = dict(value)
            for key in (
                "community",
                "filings_by_ticker",
                "filing_counts",
                "market_events_by_ticker",
            ):
                copy[key] = {ticker: row for ticker, row in copy[key].items() if ticker in universe}
            return copy

        reports.append(
            compare(
                "Cold Pulse loader / 15000 filings / 1500 tickers / 50 candidates",
                lambda: old_loader(at=now),
                lambda: main._pulse_scoring_inputs(at=now),
                args.repeats,
                normalize=scoped,
            )
        )
    raw = sqlite3.connect(":memory:")
    try:
        handle = DatabaseConnection(raw, "sqlite")
        handle.execute(
            "CREATE TABLE scan_outcomes(snapshot_id TEXT PRIMARY KEY,ticker TEXT,"
            "base_at TEXT,next_attempt_at TEXT,barrier_label TEXT,return_60m_pct REAL,"
            "return_1h_pct REAL,return_1d_pct REAL,return_5d_pct REAL,payload TEXT)"
        )
        handle.execute("CREATE INDEX scan_outcomes_base_at ON scan_outcomes(base_at DESC)")
        handle.executemany(
            "INSERT INTO scan_outcomes VALUES(?,?,?,NULL,'up',1,1,1,NULL,?)",
            [
                (f"s{i:06}", "A", (now - timedelta(days=6)).isoformat(), "x" * 300)
                for i in range(args.rows)
            ],
        )
        cutoff = (now - timedelta(days=10)).isoformat()
        reports.append(
            compare(
                "Oldest-due queue selection / synthetic SQLite",
                lambda: legacy_queue(handle, now, cutoff, 200),
                lambda: outcomes._pending_scan_outcomes(handle, now, cutoff, limit=200)[:2],
                args.repeats,
            )
        )
        _, total, examined = outcomes._pending_scan_outcomes(handle, now, cutoff, limit=200)
        reports[-1].update(legacy_rows_materialized=total, optimized_rows_materialized=examined)
    finally:
        raw.close()
    templates = [
        "SELECT * FROM scan_outcomes WHERE base_at>=? AND "
        "(next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY base_at,snapshot_id LIMIT ?",
        "UPDATE kol_calls SET max_favorable_pct=MAX(max_favorable_pct,?) WHERE id=?",
        "INSERT OR IGNORE INTO worker_state(key,value,updated_at) VALUES(?,?,?)",
    ]
    reports.append(
        compare(
            "3000 PostgreSQL template translations (not database latency)",
            lambda: [postgres_statement.__wrapped__(s) for s in templates * 1000],
            lambda: [postgres_statement(s) for s in templates * 1000],
            args.repeats,
        )
    )
    return {
        "synthetic_benchmark": True,
        "production_latency_claim": False,
        "baseline_ref": args.baseline_ref,
        "repeats": args.repeats,
        "results": reports,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="7e44356")
    parser.add_argument("--rows", type=int, default=50_000)
    parser.add_argument("--repeats", type=int, default=5)
    arguments = parser.parse_args()
    if arguments.rows < 200 or arguments.repeats < 1:
        parser.error("--rows must be at least 200; --repeats must be positive")
    print(json.dumps(run(arguments), indent=2))
