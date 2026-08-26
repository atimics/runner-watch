from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.db import connection

PASSIVE_TRACKING_TABLES = (
    "activity_events",
    "ticker_hearts",
    "radar_seen",
    "pulse_profile_state",
    "ticker_reactions",
)


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _tables(database: Any) -> set[str]:
    if database.backend == "postgres":
        rows = database.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema()"
        ).fetchall()
        return {str(row["table_name"]) for row in rows}
    rows = database.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def _rows(
    database: Any,
    tables: set[str],
    table: str,
    where: str,
    parameters: tuple[Any, ...],
    *,
    order_by: str = "",
) -> list[dict[str, Any]]:
    if table not in tables:
        return []
    statement = f"SELECT * FROM {table} WHERE {where}"
    if order_by:
        statement += f" ORDER BY {order_by}"
    return [dict(row) for row in database.execute(statement, parameters).fetchall()]


def _public_passkeys(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = (
        "device_type",
        "backed_up",
        "transports",
        "created_at",
        "last_used_at",
    )
    return [{key: row.get(key) for key in allowed} for row in rows]


def export_user_data(user_id: str) -> dict[str, Any]:
    """Return a portable copy without authentication secrets or session hashes."""

    profile = f"u:{user_id}"
    with connection() as database:
        tables = _tables(database)
        account_rows = _rows(database, tables, "users", "id=?", (user_id,))
        if not account_rows:
            raise KeyError("Account not found")
        passkeys = _rows(database, tables, "passkeys", "user_id=?", (user_id,))
        sessions = _rows(database, tables, "sessions", "user_id=?", (user_id,))
        cases = _rows(
            database,
            tables,
            "thesis_cases",
            "user_id=?",
            (user_id,),
            order_by="created_at",
        )
        case_ids = [str(row["id"]) for row in cases]
        commissions = _rows(
            database,
            tables,
            "research_commissions",
            "user_id=?",
            (user_id,),
            order_by="created_at",
        )
        commission_ids = [str(row["id"]) for row in commissions]
        caller_identities = _rows(
            database,
            tables,
            "caller_identities",
            "user_id=?",
            (user_id,),
            order_by="claimed_at",
        )
        caller_identity_ids = [str(row["id"]) for row in caller_identities]

        def related(table: str, column: str, values: list[str]) -> list[dict[str, Any]]:
            if not values or table not in tables:
                return []
            placeholders = ",".join("?" for _ in values)
            return [
                dict(row)
                for row in database.execute(
                    f"SELECT * FROM {table} WHERE {column} IN ({placeholders})",
                    tuple(values),
                ).fetchall()
            ]

        return {
            "exported_at": _iso(),
            "account": account_rows[0],
            "passkeys": _public_passkeys(passkeys),
            "sessions": [
                {
                    "created_at": row.get("created_at"),
                    "expires_at": row.get("expires_at"),
                }
                for row in sessions
            ],
            "comments": _rows(
                database,
                tables,
                "ticker_comments",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "public_thread_aliases": _rows(
                database,
                tables,
                "public_aliases",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "caller_identities": caller_identities,
            "caller_identity_claims": _rows(
                database,
                tables,
                "caller_identity_claims",
                "user_id=?",
                (user_id,),
                order_by="claimed_at",
            ),
            "community_calls": related(
                "community_calls", "caller_identity_id", caller_identity_ids
            ),
            "signals": _rows(
                database,
                tables,
                "signals",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "reports_submitted": _rows(
                database,
                tables,
                "reports",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "watchlist": _rows(
                database,
                tables,
                "watches",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "positions": _rows(
                database,
                tables,
                "user_positions",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "cases": cases,
            "case_revisions": related("thesis_case_revisions", "case_id", case_ids),
            "case_updates": related("thesis_case_updates", "case_id", case_ids),
            "case_outcomes": related("thesis_case_outcomes", "case_id", case_ids),
            "research": commissions,
            "research_stages": related(
                "research_stage_runs", "commission_id", commission_ids
            ),
            "legacy_tracking": {
                "activity": _rows(
                    database, tables, "activity_events", "profile_id=?", (profile,)
                ),
                "radar_seen": _rows(
                    database, tables, "radar_seen", "profile_id=?", (profile,)
                ),
                "pulse_attention": _rows(
                    database, tables, "pulse_profile_state", "profile_id=?", (profile,)
                ),
                "reactions": _rows(
                    database, tables, "ticker_reactions", "profile_id=?", (profile,)
                ),
            },
        }


def purge_passive_tracking() -> dict[str, int]:
    """Delete passive behavioural profiles; the product no longer writes them."""

    deleted: dict[str, int] = {}
    with connection() as database:
        tables = _tables(database)
        for table in PASSIVE_TRACKING_TABLES:
            deleted[table] = (
                database.execute(f"DELETE FROM {table}").rowcount if table in tables else 0
            )
    return deleted


def prune_personal_data(at: datetime | None = None) -> dict[str, int]:
    """Apply the short technical retention periods defined in the privacy notice."""

    timestamp = at or datetime.now(UTC)
    deleted = purge_passive_tracking()
    with connection() as database:
        tables = _tables(database)
        if "sessions" in tables:
            deleted["sessions"] = database.execute(
                "DELETE FROM sessions WHERE expires_at<=?", (_iso(timestamp),)
            ).rowcount
        if "auth_challenges" in tables:
            deleted["auth_challenges"] = database.execute(
                "DELETE FROM auth_challenges WHERE expires_at<=?", (_iso(timestamp),)
            ).rowcount
        if "stripe_webhook_events" in tables:
            deleted["stripe_webhook_events"] = database.execute(
                "DELETE FROM stripe_webhook_events WHERE received_at<?",
                (_iso(timestamp - timedelta(days=400)),),
            ).rowcount
        if "users" in tables and "passkeys" in tables:
            deleted["pending_users"] = database.execute(
                "DELETE FROM users WHERE status='pending' AND created_at<? "
                "AND NOT EXISTS(SELECT 1 FROM passkeys p WHERE p.user_id=users.id)",
                (_iso(timestamp - timedelta(minutes=15)),),
            ).rowcount
    return deleted


def delete_user_data(user_id: str) -> dict[str, Any]:
    """Delete one account and all local data that can identify or describe it."""

    profile = f"u:{user_id}"
    with connection() as database:
        tables = _tables(database)
        if "users" not in tables:
            return {"deleted": False}
        exists = database.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
        if not exists:
            return {"deleted": False}

        def delete(table: str, where: str, parameters: tuple[Any, ...]) -> None:
            if table in tables:
                database.execute(f"DELETE FROM {table} WHERE {where}", parameters)

        # Remove dependent records before older foreign keys that do not cascade.
        delete(
            "research_stage_runs",
            "commission_id IN (SELECT id FROM research_commissions WHERE user_id=?)",
            (user_id,),
        )
        delete("research_commissions", "user_id=?", (user_id,))
        delete(
            "reports",
            "user_id=? OR signal_id IN (SELECT id FROM signals WHERE user_id=?)",
            (user_id, user_id),
        )
        delete("signals", "user_id=?", (user_id,))
        delete(
            "thesis_case_seen",
            "user_id=? OR case_id IN (SELECT id FROM thesis_cases WHERE user_id=?)",
            (user_id, user_id),
        )
        for table in (
            "thesis_case_claims",
            "thesis_case_outcomes",
            "thesis_case_updates",
            "thesis_case_revisions",
        ):
            delete(
                table,
                "case_id IN (SELECT id FROM thesis_cases WHERE user_id=?)",
                (user_id,),
            )
        delete("thesis_cases", "user_id=?", (user_id,))
        delete("ticker_comments", "user_id=?", (user_id,))
        delete("public_aliases", "user_id=?", (user_id,))
        delete("comment_pseudonyms", "user_id=?", (user_id,))
        if "caller_identities" in tables:
            caller_ids = [
                str(row["id"])
                for row in database.execute(
                    "SELECT id FROM caller_identities WHERE user_id=?", (user_id,)
                ).fetchall()
            ]
            for caller_id in caller_ids:
                delete("community_calls", "caller_identity_id=?", (caller_id,))
                database.execute(
                    """
                    UPDATE caller_identities
                    SET user_id=NULL,status='tombstoned',claim_cost_cents=NULL,
                        payment_reference=NULL,claimed_at=NULL,deleted_at=?
                    WHERE id=?
                    """,
                    (_iso(), caller_id),
                )
        delete("caller_identity_claims", "user_id=?", (user_id,))
        delete("user_positions", "user_id=?", (user_id,))
        delete("watches", "user_id=?", (user_id,))
        for table in PASSIVE_TRACKING_TABLES:
            delete(table, "profile_id=?", (profile,))
        delete("sessions", "user_id=?", (user_id,))
        delete("auth_challenges", "user_id=?", (user_id,))
        delete("passkeys", "user_id=?", (user_id,))
        database.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"deleted": True}
