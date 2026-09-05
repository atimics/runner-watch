from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from runner_web.db import connection

HISTORY_DAYS = 7
MAX_HISTORY_POINTS = 250_000
MAX_DETAIL_HISTORY = 2016


def save_memecoin_snapshot(
    rows: list[dict[str, Any]], *, run_id: str, collected_at: datetime
) -> None:
    collected = collected_at.isoformat()
    oldest = (collected_at - timedelta(days=HISTORY_DAYS)).isoformat()
    with connection() as database:
        for row in rows:
            database.execute(
                """
                INSERT INTO memecoin_assets(coin_id,quote_json,collected_at,run_id)
                VALUES(?,?,?,?) ON CONFLICT(coin_id) DO UPDATE SET
                    quote_json=excluded.quote_json,collected_at=excluded.collected_at,
                    run_id=excluded.run_id
                WHERE memecoin_assets.collected_at<=excluded.collected_at
                """,
                (row["id"], json.dumps(row, allow_nan=False), collected, run_id),
            )
            observed = row.get("observed_at")
            if observed is None:
                continue
            observed_time = datetime.fromisoformat(observed)
            if not -60 <= (collected_at - observed_time).total_seconds() <= HISTORY_DAYS * 86400:
                continue
            database.execute(
                """
                INSERT INTO memecoin_quote_history(coin_id,observed_at,collected_at,price,run_id)
                VALUES(?,?,?,?,?) ON CONFLICT(coin_id,observed_at) DO UPDATE SET
                    collected_at=excluded.collected_at,price=excluded.price,run_id=excluded.run_id
                WHERE memecoin_quote_history.collected_at<=excluded.collected_at
                """,
                (row["id"], observed, collected, row["price"], run_id),
            )
        database.execute("DELETE FROM memecoin_quote_history WHERE observed_at<?", (oldest,))
        database.execute(
            """
            DELETE FROM memecoin_quote_history WHERE (coin_id,observed_at) NOT IN (
                SELECT coin_id,observed_at FROM memecoin_quote_history
                ORDER BY observed_at DESC,coin_id LIMIT ?
            )
            """,
            (MAX_HISTORY_POINTS,),
        )
        for key, value in (
            (
                "memecoins_snapshot",
                {"rows": rows, "collected_at": collected, "run_id": run_id},
            ),
            ("memecoins_error", None),
        ):
            database.execute(
                """
                INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                WHERE worker_state.updated_at<=excluded.updated_at
                """,
                (key, json.dumps(value, allow_nan=False), collected),
            )


def stored_memecoin(coin_id: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            "SELECT quote_json,collected_at,run_id FROM memecoin_assets WHERE coin_id=?",
            (coin_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "coin": json.loads(row["quote_json"]),
        "collected_at": row["collected_at"],
        "run_id": row["run_id"],
    }


def memecoin_history(coin_id: str, *, at: datetime, limit: int = 288) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), MAX_DETAIL_HISTORY))
    with connection() as database:
        rows = database.execute(
            """
            SELECT observed_at,collected_at,price FROM memecoin_quote_history
            WHERE coin_id=? AND observed_at>=? AND observed_at<=?
            ORDER BY observed_at DESC LIMIT ?
            """,
            (
                coin_id,
                (at - timedelta(days=HISTORY_DAYS)).isoformat(),
                (at + timedelta(seconds=60)).isoformat(),
                bounded_limit,
            ),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]
