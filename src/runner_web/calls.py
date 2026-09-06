from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

from runner_web.caller_ids import ensure_caller_identity_with_database
from runner_web.db import connection
from runner_web.flash_wallet import credit_flash, runner_call_reward


def _iso() -> str:
    return datetime.now(UTC).isoformat()


def _public_id() -> str:
    return secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]


def _call(row: Any, current_price: float | None = None) -> dict[str, Any]:
    stored = dict(row)
    item = {
        key: stored.get(key)
        for key in (
            "public_id",
            "ticker",
            "side",
            "entry_price",
            "entry_at",
            "exit_price",
            "exit_at",
            "status",
            "created_at",
            "updated_at",
            "caller_handle",
        )
    }
    mark_price = item["exit_price"] if item["status"] == "closed" else current_price
    item["mark_price"] = float(mark_price) if mark_price is not None else None
    item["return_pct"] = (
        round((float(mark_price) / float(item["entry_price"]) - 1) * 100, 2)
        if mark_price is not None
        else None
    )
    item["flash_reward"] = int(stored.get("flash_reward") or 0)
    item["projected_flash_reward"] = (
        runner_call_reward(item["return_pct"])
        if item["status"] == "active"
        else item["flash_reward"]
    )
    item["reward_label"] = f"+{item['flash_reward']} Flash" if item["flash_reward"] > 0 else None
    return item


def create_call(
    user_id: str,
    ticker: str,
    *,
    entry_price: float,
    entry_at: str,
) -> dict[str, Any]:

    call_id = str(uuid.uuid4())
    timestamp = _iso()
    with connection() as db:
        identity = ensure_caller_identity_with_database(db, user_id)
        existing = db.execute(
            """
            SELECT c.*,ci.handle AS caller_handle,
                   COALESCE(ft.amount,0) AS flash_reward
            FROM community_calls c
            JOIN caller_identities ci ON ci.id=c.caller_identity_id
            LEFT JOIN flash_transactions ft
              ON ft.user_id=c.user_id AND ft.kind='runner_call_win' AND ft.reference_id=c.id
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
                id,public_id,user_id,caller_identity_id,ticker,side,entry_price,entry_at,status,
                created_at,updated_at
            ) VALUES(?,?,?,?,?, 'long',?,?,'active',?,?) ON CONFLICT DO NOTHING
            """,
            (
                call_id,
                _public_id(),
                user_id,
                identity["id"],
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
                SELECT c.*,ci.handle AS caller_handle,
                       COALESCE(ft.amount,0) AS flash_reward
                FROM community_calls c
                JOIN caller_identities ci ON ci.id=c.caller_identity_id
                LEFT JOIN flash_transactions ft
                  ON ft.user_id=c.user_id AND ft.kind='runner_call_win'
                 AND ft.reference_id=c.id
                WHERE c.id=?
                """,
                (call_id,),
            ).fetchone()
        else:
            row = db.execute(
                """
                SELECT c.*,ci.handle AS caller_handle,
                       COALESCE(ft.amount,0) AS flash_reward
                FROM community_calls c
                JOIN caller_identities ci ON ci.id=c.caller_identity_id
                LEFT JOIN flash_transactions ft
                  ON ft.user_id=c.user_id AND ft.kind='runner_call_win'
                 AND ft.reference_id=c.id
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
            SELECT c.*,ci.handle AS caller_handle,
                   COALESCE(ft.amount,0) AS flash_reward
            FROM community_calls c
            JOIN caller_identities ci ON ci.id=c.caller_identity_id
            LEFT JOIN flash_transactions ft
              ON ft.user_id=c.user_id AND ft.kind='runner_call_win' AND ft.reference_id=c.id
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
            SELECT c.*,ci.handle AS caller_handle,
                   COALESCE(ft.amount,0) AS flash_reward
            FROM community_calls c
            JOIN caller_identities ci ON ci.id=c.caller_identity_id
            LEFT JOIN flash_transactions ft
              ON ft.user_id=c.user_id AND ft.kind='runner_call_win' AND ft.reference_id=c.id
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
        return_pct = (float(exit_price) / float(existing["entry_price"]) - 1) * 100
        reward = runner_call_reward(return_pct)
        if reward:
            credit_flash(
                db,
                user_id,
                reward,
                kind="runner_call_win",
                reference_id=str(existing["id"]),
            )
        row = db.execute(
            """
            SELECT c.*,ci.handle AS caller_handle,
                   COALESCE(ft.amount,0) AS flash_reward
            FROM community_calls c
            JOIN caller_identities ci ON ci.id=c.caller_identity_id
            LEFT JOIN flash_transactions ft
              ON ft.user_id=c.user_id AND ft.kind='runner_call_win' AND ft.reference_id=c.id
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
            SELECT c.*,ci.handle AS caller_handle,
                   COALESCE(ft.amount,0) AS flash_reward
            FROM community_calls c
            JOIN caller_identities ci ON ci.id=c.caller_identity_id
            LEFT JOIN flash_transactions ft
              ON ft.user_id=c.user_id AND ft.kind='runner_call_win' AND ft.reference_id=c.id
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
            SELECT c.*,ci.handle AS caller_handle,
                   COALESCE(ft.amount,0) AS flash_reward
            FROM community_calls c
            JOIN caller_identities ci ON ci.id=c.caller_identity_id
            LEFT JOIN flash_transactions ft
              ON ft.user_id=c.user_id AND ft.kind='runner_call_win' AND ft.reference_id=c.id
            ORDER BY c.updated_at DESC,c.id DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
    marks = current_prices or {}
    return [_call(row, marks.get(str(row["ticker"]))) for row in rows]


def caller_call_rows(
    caller_handle: str,
    *,
    limit: int = 200,
) -> list[Any] | None:
    """Fetch one caller's raw call rows, or None when the handle is unknown."""
    with connection() as db:
        identity = db.execute(
            """
            SELECT id,handle FROM caller_identities
            WHERE handle=? AND status='active'
            """,
            (caller_handle,),
        ).fetchone()
        if not identity:
            return None
        return db.execute(
            """
            SELECT c.*,? AS caller_handle,COALESCE(ft.amount,0) AS flash_reward
            FROM community_calls c
            LEFT JOIN flash_transactions ft
              ON ft.user_id=c.user_id AND ft.kind='runner_call_win' AND ft.reference_id=c.id
            WHERE c.caller_identity_id=?
            ORDER BY c.updated_at DESC,c.id DESC LIMIT ?
            """,
            (caller_handle, identity["id"], max(1, min(limit, 500))),
        ).fetchall()


def calls_from_rows(
    rows: list[Any],
    current_prices: dict[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    marks = current_prices or {}
    return [_call(row, marks.get(str(row["ticker"]))) for row in rows]


def caller_calls(
    caller_handle: str,
    *,
    current_prices: dict[str, float | None] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]] | None:
    rows = caller_call_rows(caller_handle, limit=limit)
    if rows is None:
        return None
    return calls_from_rows(rows, current_prices)


def call_stats(calls: list[dict[str, Any]]) -> dict[str, Any]:
    returns = [float(item["return_pct"]) for item in calls if item.get("return_pct") is not None]
    closed = [item for item in calls if item["status"] == "closed"]
    closed_returns = [
        float(item["return_pct"]) for item in closed if item.get("return_pct") is not None
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


def caller_summary_for_user(user_id: str) -> dict[str, Any]:

    with connection() as database:
        identity = database.execute(
            """
            SELECT id,handle FROM caller_identities
            WHERE user_id=? AND status='active'
            ORDER BY claimed_at,id LIMIT 1
            """,
            (user_id,),
        ).fetchone()
        if not identity:
            return {"handle": None, **call_stats([])}
        rows = database.execute(
            """
            SELECT c.*,? AS caller_handle FROM community_calls c
            WHERE c.user_id=? AND c.caller_identity_id=?
            ORDER BY c.updated_at DESC,c.id DESC
            """,
            (identity["handle"], user_id, identity["id"]),
        ).fetchall()
        coin_rows = database.execute(
            "SELECT coin_id,status,entry_price,exit_price FROM memecoin_calls "
            "WHERE user_id=? AND caller_identity_id=?",
            (user_id, identity["id"]),
        ).fetchall()
        sports_rows = database.execute(
            "SELECT event_id,status,result FROM sports_picks "
            "WHERE user_id=? AND caller_identity_id=?",
            (user_id, identity["id"]),
        ).fetchall()
    calls = [_call(row) for row in rows]
    stats = call_stats(calls)
    stats["total"] += len(coin_rows) + len(sports_rows)
    stats["open"] += sum(row["status"] == "active" for row in coin_rows)
    stats["open"] += sum(row["status"] == "open" for row in sports_rows)
    stats["closed"] += sum(row["status"] == "closed" for row in coin_rows)
    stats["closed"] += sum(row["status"] != "open" for row in sports_rows)
    stats["wins"] += sum(
        row["status"] == "closed" and row["exit_price"] > row["entry_price"] for row in coin_rows
    ) + sum(row["result"] == "win" for row in sports_rows)
    stats["losses"] += sum(
        row["status"] == "closed" and row["exit_price"] < row["entry_price"] for row in coin_rows
    ) + sum(row["result"] == "loss" for row in sports_rows)
    for field in ("average_return_pct", "median_return_pct", "best_return_pct", "worst_return_pct"):
        stats[field] = None
    return {"handle": str(identity["handle"]), **stats}


STOCK_CALL_MAX_AGE_DAYS = 30


def expire_stock_calls(
    at: datetime | None = None,
    *,
    max_age_days: int = STOCK_CALL_MAX_AGE_DAYS,
) -> list[str]:
    """Settle stock Calls left open past the expiry window at the latest mark.

    Returns the caller handles whose public records changed. A Call with no
    stored mark at or after its entry time stays open until a mark arrives.
    """
    current = at or datetime.now(UTC)
    cutoff = (current - timedelta(days=max_age_days)).isoformat()
    handles: list[str] = []
    with connection() as database:
        rows = database.execute(
            """
            SELECT c.*,ci.handle AS caller_handle
            FROM community_calls c
            JOIN caller_identities ci ON ci.id=c.caller_identity_id
            WHERE c.status='active' AND c.entry_at<?
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            mark = database.execute(
                """
                SELECT price,captured_at FROM scan_snapshots
                WHERE ticker=? AND captured_at>=?
                ORDER BY captured_at DESC LIMIT 1
                """,
                (str(row["ticker"]), str(row["entry_at"])),
            ).fetchone()
            if not mark or mark["price"] is None:
                continue
            changed = database.execute(
                """
                UPDATE community_calls
                SET exit_price=?,exit_at=?,status='closed',updated_at=?
                WHERE id=? AND status='active'
                """,
                (
                    float(mark["price"]),
                    str(mark["captured_at"]),
                    current.isoformat(),
                    str(row["id"]),
                ),
            )
            if changed.rowcount != 1:
                continue
            handles.append(str(row["caller_handle"]))
            return_pct = (float(mark["price"]) / float(row["entry_price"]) - 1) * 100
            reward = runner_call_reward(return_pct)
            if reward:
                credit_flash(
                    database,
                    str(row["user_id"]),
                    reward,
                    kind="runner_call_win",
                    reference_id=str(row["id"]),
                    at=current,
                )
    return handles
