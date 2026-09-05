from __future__ import annotations

import json
import math
import secrets
from datetime import UTC, datetime
from typing import Any

from runner_web import memecoins
from runner_web.caller_ids import ensure_caller_identity_with_database
from runner_web.db import connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fresh_mark(coin_id: str) -> dict[str, Any]:
    detail = memecoins.memecoin_detail(coin_id)
    if detail is None:
        raise LookupError("Coin not found")
    coin = detail["coin"]
    if detail["status"] != "ok" or coin["stale"]:
        raise ValueError("A source quote from the last 15 minutes is required for a paper Call.")
    price = float(coin["price"])
    observed = datetime.fromisoformat(str(coin["observed_at"]))
    age = (datetime.now(UTC) - observed).total_seconds()
    if not math.isfinite(price) or price <= 0 or not -60 <= age <= memecoins.STALE_SECONDS:
        raise ValueError("A current source quote is required for a paper Call.")
    return {
        "coin_id": coin_id,
        "symbol": coin["symbol"],
        "name": coin["name"],
        "price": price,
        "observed_at": observed.astimezone(UTC).isoformat(),
        "collected_at": detail["collected_at"],
        "source_url": coin["source_url"],
        "run_id": detail["evidence"].get("run_id"),
    }


def _call(row: Any, mark: dict[str, Any] | None = None) -> dict[str, Any]:
    saved = dict(row)
    item = {
        key: saved.get(key)
        for key in (
            "public_id",
            "coin_id",
            "symbol",
            "name",
            "caller_handle",
            "status",
            "entry_price",
            "entry_at",
            "exit_price",
            "exit_at",
            "created_at",
            "updated_at",
        )
    }
    price = (
        item["exit_price"]
        if item["status"] == "closed"
        else (mark.get("price") if mark and not mark.get("stale", True) else None)
    )
    item["mark_price"] = price
    item["mark_at"] = (
        item["exit_at"]
        if item["status"] == "closed"
        else (mark.get("observed_at") if price is not None and mark else None)
    )
    change = (float(price) / float(item["entry_price"]) - 1) * 100 if price is not None else None
    item["return_pct"] = round(change, 2) if change is not None and math.isfinite(change) else None
    for field in ("entry_price", "exit_price", "mark_price"):
        item[f"{field}_label"] = (
            memecoins._price_label(float(item[field])) if item[field] is not None else "—"
        )
    item["detail_url"] = f"/memecoins/coin/{item['coin_id']}"
    item["entry_evidence"] = json.loads(saved["entry_evidence"])
    item["exit_evidence"] = json.loads(saved["exit_evidence"]) if saved["exit_evidence"] else None
    return item


_SELECT = """
    SELECT c.*,ci.handle AS caller_handle,
           q.quote_json AS current_quote_json,q.collected_at AS current_collected_at
    FROM memecoin_calls c
    JOIN caller_identities ci ON ci.id=c.caller_identity_id AND ci.status='active'
    LEFT JOIN memecoin_assets q ON q.coin_id=c.coin_id
"""


def create_memecoin_call(user_id: str, coin_id: str) -> dict[str, Any]:
    mark = _fresh_mark(coin_id)
    timestamp = _now()
    with connection() as database:
        identity = ensure_caller_identity_with_database(database, user_id)
        database.execute(
            """
            INSERT INTO memecoin_calls(
                public_id,user_id,caller_identity_id,coin_id,symbol,name,status,
                entry_price,entry_at,entry_evidence,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'active',?,?,?,?,?) ON CONFLICT DO NOTHING
            """,
            (
                secrets.token_urlsafe(12),
                user_id,
                identity["id"],
                coin_id,
                mark["symbol"],
                mark["name"],
                mark["price"],
                mark["observed_at"],
                json.dumps(mark, allow_nan=False),
                timestamp,
                timestamp,
            ),
        )
        row = database.execute(
            _SELECT + " WHERE c.user_id=? AND c.coin_id=? AND c.status='active'",
            (user_id, coin_id),
        ).fetchone()
    return _call(row, {**mark, "stale": False})


def close_memecoin_call(user_id: str, public_id: str) -> dict[str, Any] | None:
    with connection() as database:
        existing = database.execute(
            _SELECT + " WHERE c.user_id=? AND c.public_id=?", (user_id, public_id)
        ).fetchone()
    if existing is None:
        return None
    if existing["status"] == "closed":
        return _call(existing)
    mark = _fresh_mark(str(existing["coin_id"]))
    if datetime.fromisoformat(mark["observed_at"]) < datetime.fromisoformat(existing["entry_at"]):
        raise ValueError(
            "A source quote at or after the entry time is required to close this Call."
        )
    with connection() as database:
        database.execute(
            """
            UPDATE memecoin_calls SET status='closed',exit_price=?,exit_at=?,exit_evidence=?,
                updated_at=? WHERE public_id=? AND user_id=? AND status='active'
            """,
            (
                mark["price"],
                mark["observed_at"],
                json.dumps(mark, allow_nan=False),
                _now(),
                public_id,
                user_id,
            ),
        )
        row = database.execute(
            _SELECT + " WHERE c.user_id=? AND c.public_id=?", (user_id, public_id)
        ).fetchone()
    return _call(row) if row else None


def memecoin_calls(
    *,
    coin_id: str | None = None,
    caller_handle: str | None = None,
    user_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses, parameters = [], []
    for field, value in (
        ("c.coin_id", coin_id),
        ("ci.handle", caller_handle),
        ("c.user_id", user_id),
    ):
        if value is not None:
            clauses.append(f"{field}=?")
            parameters.append(value)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with connection() as database:
        rows = database.execute(
            _SELECT + where + " ORDER BY c.updated_at DESC,c.public_id DESC LIMIT ?",
            (*parameters, max(1, min(limit, 500))),
        ).fetchall()
    current = datetime.now(UTC)
    enabled = memecoins.memecoins_enabled()
    return [
        _call(
            row,
            memecoins._quote_display(
                json.loads(row["current_quote_json"]), row["current_collected_at"], current
            )
            if enabled and row["current_quote_json"]
            else None,
        )
        for row in rows
    ]


def active_memecoin_call(user_id: str, coin_id: str) -> dict[str, Any] | None:
    return next(
        (
            call
            for call in memecoin_calls(user_id=user_id, coin_id=coin_id)
            if call["status"] == "active"
        ),
        None,
    )
