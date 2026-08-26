from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from runner_web.db import connection


def _iso() -> str:
    return datetime.now(UTC).isoformat()


def _position(row: Any, current_price: float | None = None) -> dict[str, Any]:
    item = dict(row)
    mark_price = item["exit_price"] if item["status"] == "closed" else current_price
    item["mark_price"] = mark_price
    item["return_pct"] = (
        round((float(mark_price) / float(item["entry_price"]) - 1) * 100, 2)
        if mark_price is not None
        else None
    )
    return item


def positions_for_ticker(
    user_id: str,
    ticker: str,
    *,
    current_price: float | None = None,
) -> list[dict[str, Any]]:
    with connection() as db:
        rows = db.execute(
            """
            SELECT * FROM user_positions
            WHERE user_id=? AND ticker=?
            ORDER BY status ASC,entry_at DESC,created_at DESC
            """,
            (user_id, ticker),
        ).fetchall()
    return [_position(row, current_price) for row in rows]


def position_for_user(user_id: str, position_id: str) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            "SELECT * FROM user_positions WHERE id=? AND user_id=?",
            (position_id, user_id),
        ).fetchone()
    return _position(row) if row else None


def create_position(
    user_id: str,
    ticker: str,
    *,
    entry_price: float,
    entry_at: str,
) -> dict[str, Any]:
    position_id = str(uuid.uuid4())
    timestamp = _iso()
    with connection() as db:
        db.execute(
            """
            INSERT INTO user_positions(
                id,user_id,ticker,entry_price,entry_at,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,'active',?,?)
            """,
            (position_id, user_id, ticker, entry_price, entry_at, timestamp, timestamp),
        )
        row = db.execute(
            "SELECT * FROM user_positions WHERE id=? AND user_id=?",
            (position_id, user_id),
        ).fetchone()
    return _position(row, entry_price)


def close_position(
    user_id: str,
    position_id: str,
    *,
    exit_price: float,
    exit_at: str,
) -> dict[str, Any] | None:
    timestamp = _iso()
    with connection() as db:
        existing = db.execute(
            "SELECT * FROM user_positions WHERE id=? AND user_id=?",
            (position_id, user_id),
        ).fetchone()
        if not existing or str(existing["status"]) != "active":
            return None
        if str(exit_at) < str(existing["entry_at"]):
            raise ValueError("Exit time cannot be before entry time")
        db.execute(
            """
            UPDATE user_positions
            SET exit_price=?,exit_at=?,status='closed',updated_at=?
            WHERE id=? AND user_id=? AND status='active'
            """,
            (exit_price, exit_at, timestamp, position_id, user_id),
        )
        row = db.execute(
            "SELECT * FROM user_positions WHERE id=? AND user_id=?",
            (position_id, user_id),
        ).fetchone()
    return _position(row)
