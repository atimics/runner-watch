from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from runner_web import db
from runner_web.sec_training import _canonical_json


def runner_braid_issuer_universe() -> dict[str, Any]:
    with db.connection() as database:
        rows = database.execute(
            """
            SELECT c.cik,c.ticker,c.name
            FROM sec_companies c
            JOIN (SELECT DISTINCT UPPER(ticker) AS ticker FROM scan_snapshots) s
              ON s.ticker=UPPER(c.ticker)
            ORDER BY c.cik,UPPER(c.ticker),c.name
            """
        ).fetchall()
    grouped: dict[int, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        cik = int(row["cik"])
        issuer = grouped.setdefault(
            cik,
            {"cik": cik, "company": str(row["name"]), "tickers": []},
        )
        ticker = str(row["ticker"]).strip().upper()
        if ticker and ticker not in issuer["tickers"]:
            issuer["tickers"].append(ticker)
    if not grouped:
        raise ValueError("Runner Watch has no SEC issuers in its current scan universe")
    return {
        "schemaVersion": "braid.sec-issuer-universe/v1",
        "issuers": list(grouped.values()),
    }


def export_braid_issuer_universe(output: Path) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"issuer universe output already exists: {output}")
    universe = runner_braid_issuer_universe()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_canonical_json(universe) + "\n", encoding="utf-8", newline="\n")
    return universe


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export Runner Watch's current issuer universe for the Braid SEC worker"
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--database-path", type=Path)
    arguments = parser.parse_args()
    if arguments.database_path:
        if db.DATABASE_URL:
            parser.error("--database-path cannot be combined with DATABASE_URL")
        db.DATABASE_PATH = arguments.database_path
    universe = export_braid_issuer_universe(arguments.output)
    print(
        _canonical_json(
            {
                "output": str(arguments.output),
                "issuers": len(universe["issuers"]),
            }
        )
    )


if __name__ == "__main__":
    main()
