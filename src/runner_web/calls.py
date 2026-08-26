from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime
from statistics import median
from typing import Any

from runner_web.db import connection


def _iso() -> str:
    return datetime.now(UTC).isoformat()


def _public_id() -> str:
    return secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]


def _call(row: Any, current_price: float | None = None) -> dict[str, Any]:
    item = dict(row)
    mark_price = item["exit_price"] if item["status"] == "closed" else current_price
    item["mark_price"] = float(mark_price) if mark_price is not None else None
    item["return_pct"] = (
        round((float(mark_price) / float(item["entry_price"]) - 1) * 100, 2)
        if mark_price is not None
        else None
    )
    return item


def create_call(
    user_id: str,
    ticker: str,
    *,
    entry_price: float,
    entry_at: str,
) -> dict[str, Any]:
    """Create an immutable public Call using a server supplied market mark."""

    call_id = str(uuid.uuid4())
    timestamp = _iso()
    with connection() as db:
        existing = db.execute(
            """
            SELECT c.*,p.pseudonym FROM community_calls c
            JOIN comment_pseudonyms p ON p.user_id=c.user_id
            WHERE c.user_id=? AND c.ticker=? AND c.status='active'
            ORDER BY c.created_at DESC LIMIT 1
            """,
            (user_id, ticker),
        ).fetchone()
        if existing:
            return _call(existing, entry_price)
        inserted = db.execute(
            """
            INSERT INTO community_calls(
                id,public_id,user_id,ticker,side,entry_price,entry_at,status,
                created_at,updated_at
            ) VALUES(?,?,?,?, 'long',?,?,'active',?,?) ON CONFLICT DO NOTHING
            """,
            (
                call_id,
                _public_id(),
                user_id,
                ticker,
                entry_price,
                entry_at,
                timestamp,
                timestamp,
            ),
        )
        if inserted.rowcount == 1:
            row = db.execute(
                """
                SELECT c.*,p.pseudonym FROM community_calls c
                JOIN comment_pseudonyms p ON p.user_id=c.user_id WHERE c.id=?
                """,
                (call_id,),
            ).fetchone()
        else:
            row = db.execute(
                """
                SELECT c.*,p.pseudonym FROM community_calls c
                JOIN comment_pseudonyms p ON p.user_id=c.user_id
                WHERE c.user_id=? AND c.ticker=? AND c.status='active'
                ORDER BY c.created_at DESC LIMIT 1
                """,
                (user_id, ticker),
            ).fetchone()
    return _call(row, entry_price)


def active_call_for_user(
    user_id: str,
    ticker: str,
    *,
    current_price: float | None = None,
) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            """
            SELECT c.*,p.pseudonym FROM community_calls c
            JOIN comment_pseudonyms p ON p.user_id=c.user_id
            WHERE c.user_id=? AND c.ticker=? AND c.status='active'
            ORDER BY c.created_at DESC LIMIT 1
            """,
            (user_id, ticker),
        ).fetchone()
    return _call(row, current_price) if row else None


def call_for_user(user_id: str, public_id: str) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            """
            SELECT c.*,p.pseudonym FROM community_calls c
            JOIN comment_pseudonyms p ON p.user_id=c.user_id
            WHERE c.user_id=? AND c.public_id=?
            """,
            (user_id, public_id),
        ).fetchone()
    return _call(row) if row else None


def close_call(
    user_id: str,
    public_id: str,
    *,
    exit_price: float,
    exit_at: str,
) -> dict[str, Any] | None:
    """Close a Call once. Entry and exit marks cannot be edited afterward."""

    timestamp = _iso()
    with connection() as db:
        existing = db.execute(
            """
            SELECT * FROM community_calls
            WHERE user_id=? AND public_id=? AND status='active'
            """,
            (user_id, public_id),
        ).fetchone()
        if not existing:
            return None
        if str(exit_at) < str(existing["entry_at"]):
            raise ValueError("Exit time cannot be before entry time")
        changed = db.execute(
            """
            UPDATE community_calls
            SET exit_price=?,exit_at=?,status='closed',updated_at=?
            WHERE user_id=? AND public_id=? AND status='active'
            """,
            (exit_price, exit_at, timestamp, user_id, public_id),
        )
        if changed.rowcount != 1:
            return None
        row = db.execute(
            """
            SELECT c.*,p.pseudonym FROM community_calls c
            JOIN comment_pseudonyms p ON p.user_id=c.user_id
            WHERE c.user_id=? AND c.public_id=?
            """,
            (user_id, public_id),
        ).fetchone()
    return _call(row)


def calls_for_ticker(
    ticker: str,
    *,
    current_price: float | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    with connection() as db:
        rows = db.execute(
            """
            SELECT c.*,p.pseudonym FROM community_calls c
            JOIN comment_pseudonyms p ON p.user_id=c.user_id
            WHERE c.ticker=?
            ORDER BY CASE WHEN c.status='active' THEN 0 ELSE 1 END,
                     c.updated_at DESC,c.id DESC LIMIT ?
            """,
            (ticker, max(1, min(limit, 100))),
        ).fetchall()
    return [_call(row, current_price) for row in rows]


def recent_calls(
    *,
    current_prices: dict[str, float | None] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    with connection() as db:
        rows = db.execute(
            """
            SELECT c.*,p.pseudonym FROM community_calls c
            JOIN comment_pseudonyms p ON p.user_id=c.user_id
            ORDER BY c.updated_at DESC,c.id DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
    marks = current_prices or {}
    return [_call(row, marks.get(str(row["ticker"]))) for row in rows]


def caller_calls(
    pseudonym: str,
    *,
    current_prices: dict[str, float | None] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]] | None:
    with connection() as db:
        owner = db.execute(
            "SELECT user_id,pseudonym FROM comment_pseudonyms WHERE pseudonym=?",
            (pseudonym,),
        ).fetchone()
        if not owner:
            return None
        rows = db.execute(
            """
            SELECT c.*,? AS pseudonym FROM community_calls c
            WHERE c.user_id=? ORDER BY c.updated_at DESC,c.id DESC LIMIT ?
            """,
            (pseudonym, owner["user_id"], max(1, min(limit, 500))),
        ).fetchall()
    marks = current_prices or {}
    return [_call(row, marks.get(str(row["ticker"]))) for row in rows]


def call_stats(calls: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(item["return_pct"]) for item in calls if item.get("return_pct") is not None]
    closed = [item for item in calls if item["status"] == "closed"]
    closed_returns = [
        float(item["return_pct"])
        for item in closed
        if item.get("return_pct") is not None
    ]
    return {
        "total": len(calls),
        "open": sum(item["status"] == "active" for item in calls),
        "closed": len(closed),
        "wins": sum(value > 0 for value in closed_returns),
        "losses": sum(value < 0 for value in closed_returns),
        "average_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "median_return_pct": round(median(returns), 2) if returns else None,
        "best_return_pct": round(max(returns), 2) if returns else None,
        "worst_return_pct": round(min(returns), 2) if returns else None,
        "tickers": len({str(item["ticker"]) for item in calls}),
    }
