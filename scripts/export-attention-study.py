"""Export market-only research inputs through a read-only PostgreSQL transaction.

Run on a host with DATABASE_URL set. Standard output is a gzip JSONL stream.
Example: python scripts/export-attention-study.py bars --start 2026-08-25 \
    --end 2026-09-24 > bars.jsonl.gz
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
from datetime import date

import psycopg
from psycopg.rows import dict_row

SNAPSHOT_SQL = """
SELECT s.id AS snapshot_id,s.scan_run_id,s.ticker,s.captured_at,s.quote_time,s.session,
 s.price,s.change_pct,s.momentum_5m_pct,s.momentum_15m_pct,s.momentum_previous_5m_pct,
 s.relative_volume,s.recent_relative_volume,s.intraday_volatility_pct,s.dollar_volume,
 s.recent_dollar_volume,s.average_dollar_volume,s.stale_minutes,s.baseline_rank
FROM scan_snapshots s
WHERE s.scan_run_id IS NOT NULL AND s.captured_at >= %s AND s.captured_at < %s
ORDER BY s.captured_at,s.scan_run_id,s.ticker
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("snapshots", "bars"))
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    if not 0 < (args.end - args.start).days <= 40:
        parser.error("Choose a date window of 1 through 40 days")
    bounds = (args.start.isoformat(), args.end.isoformat())
    with (
        psycopg.connect(
            os.environ["DATABASE_URL"],
            options="-c default_transaction_read_only=on -c statement_timeout=30000",
            row_factory=dict_row,
        ) as database,
        gzip.GzipFile(fileobj=sys.stdout.buffer, mode="wb", mtime=0) as output,
    ):
        database.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")

        def emit(row: dict) -> None:
            output.write((json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n").encode())

        if args.kind == "snapshots":
            with database.cursor(name="attention_snapshots") as cursor:
                cursor.execute(SNAPSHOT_SQL, bounds)
                for row in cursor:
                    emit(row)
        else:
            symbols = database.execute(
                "SELECT DISTINCT ticker FROM scan_snapshots WHERE session='REGULAR' "
                "AND scan_run_id IS NOT NULL AND captured_at >= %s AND captured_at < %s "
                "ORDER BY ticker",
                bounds,
            ).fetchall()
            for symbol in symbols:
                # The per-symbol index keeps each archive read bounded.
                rows = database.execute(
                    "SELECT ticker,bar_time,open,high,low,close,volume,"
                    "first_collected_at,last_collected_at FROM market_bars "
                    "WHERE source='yahoo' AND ticker=%s AND interval='5m' "
                    "AND bar_time >= %s AND bar_time < %s ORDER BY bar_time",
                    (symbol["ticker"], *bounds),
                )
                for row in rows:
                    emit(row)


if __name__ == "__main__":
    main()
