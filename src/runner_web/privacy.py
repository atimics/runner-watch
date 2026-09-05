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

PORTABLE_CONTENT_GROUPS = {
    "Posts and Calls": (
        "comments",
        "sports_comments",
        "community_calls",
        "memecoin_calls",
        "sports_picks",
        "signals",
        "reports_submitted",
        "content_notices",
    ),
    "Private work": (
        "watchlist",
        "positions",
        "cases",
        "case_revisions",
        "case_updates",
        "case_outcomes",
    ),
    "Research": (
        "research",
        "research_stages",
        "flash_forecasts",
        "sports_ai_forecasts",
        "flash_forecast_outcomes",
        "flash_evaluation_events",
        "flash_report_requests",
        "model_routes",
        "model_connectors",
        "model_jobs",
    ),
}


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _tables(database: Any) -> set[str]:
    if database.backend == "postgres":
        rows = database.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()"
        ).fetchall()
        return {str(row["table_name"]) for row in rows}
    rows = database.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
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

        flash_forecasts = related("flash_forecasts", "report_id", commission_ids)
        forecast_ids = [str(row["id"]) for row in flash_forecasts]
        model_connectors = _rows(
            database,
            tables,
            "llm_edge_connectors",
            "user_id=?",
            (user_id,),
            order_by="created_at",
        )
        public_account = dict(account_rows[0])
        public_account.pop("registration_invite_hash", None)

        return {
            "exported_at": _iso(),
            "account": public_account,
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
            "sports_comments": _rows(
                database,
                tables,
                "sports_comments",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "comment_avatar": _rows(
                database,
                tables,
                "comment_avatars",
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
            "community_calls": _rows(
                database,
                tables,
                "community_calls",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "memecoin_calls": _rows(
                database,
                tables,
                "memecoin_calls",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "sports_picks": _rows(
                database,
                tables,
                "sports_picks",
                "user_id=?",
                (user_id,),
                order_by="created_at",
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
            "content_notices": _rows(
                database,
                tables,
                "content_notices",
                "report_id IN (SELECT id FROM research_commissions WHERE user_id=?) "
                "OR comment_id IN (SELECT id FROM ticker_comments WHERE user_id=?)",
                (user_id, user_id),
                order_by="created_at,id",
            ),
            "research": commissions,
            "research_stages": related("research_stage_runs", "commission_id", commission_ids),
            "flash_forecasts": flash_forecasts,
            "sports_ai_forecasts": related("sports_ai_forecasts", "report_id", commission_ids),
            "flash_forecast_outcomes": related(
                "flash_forecast_outcomes", "forecast_id", forecast_ids
            ),
            "flash_evaluation_events": related(
                "flash_evaluation_events", "forecast_id", forecast_ids
            ),
            "flash_wallet": _rows(database, tables, "flash_wallets", "user_id=?", (user_id,)),
            "flash_transactions": _rows(
                database,
                tables,
                "flash_transactions",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "flash_report_requests": _rows(
                database,
                tables,
                "flash_report_requests",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
            "model_routes": [
                {
                    key: row.get(key)
                    for key in (
                        "policy",
                        "route_kind",
                        "model",
                        "connector_id",
                        "last_checked_at",
                        "last_error",
                        "created_at",
                        "updated_at",
                    )
                }
                for row in _rows(database, tables, "user_llm_routes", "user_id=?", (user_id,))
            ],
            "model_connectors": [
                {
                    key: row.get(key)
                    for key in (
                        "id",
                        "name",
                        "status",
                        "last_seen_at",
                        "created_at",
                        "updated_at",
                    )
                }
                for row in model_connectors
            ],
            "model_jobs": _rows(
                database,
                tables,
                "llm_edge_jobs",
                "user_id=?",
                (user_id,),
                order_by="created_at",
            ),
        }


def user_data_summary(user_id: str) -> dict[str, Any]:

    exported = export_user_data(user_id)
    groups = {
        label: sum(len(exported.get(key) or []) for key in keys)
        for label, keys in PORTABLE_CONTENT_GROUPS.items()
    }
    return {
        "item_count": sum(groups.values()),
        "groups": groups,
    }


def purge_passive_tracking() -> dict[str, int]:

    deleted: dict[str, int] = {}
    with connection() as database:
        tables = _tables(database)
        for table in PASSIVE_TRACKING_TABLES:
            deleted[table] = (
                database.execute(f"DELETE FROM {table}").rowcount if table in tables else 0
            )
    return deleted


def prune_personal_data(at: datetime | None = None) -> dict[str, int]:

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


def _delete_user_content_rows(
    database: Any,
    tables: set[str],
    user_id: str,
) -> int:
    deleted = 0

    def delete(table: str, where: str, parameters: tuple[Any, ...]) -> None:
        nonlocal deleted
        if table not in tables:
            return
        rowcount = database.execute(f"DELETE FROM {table} WHERE {where}", parameters).rowcount
        if rowcount and rowcount > 0:
            deleted += rowcount

    delete(
        "content_notices",
        "report_id IN (SELECT id FROM research_commissions WHERE user_id=?) "
        "OR comment_id IN (SELECT id FROM ticker_comments WHERE user_id=?)",
        (user_id, user_id),
    )
    delete("comment_generation_requests", "user_id=?", (user_id,))
    delete("flash_report_requests", "user_id=?", (user_id,))
    delete(
        "flash_evaluation_events",
        "forecast_id IN (SELECT f.id FROM flash_forecasts f "
        "JOIN research_commissions r ON r.id=f.report_id WHERE r.user_id=?)",
        (user_id,),
    )
    delete(
        "flash_forecast_outcomes",
        "forecast_id IN (SELECT f.id FROM flash_forecasts f "
        "JOIN research_commissions r ON r.id=f.report_id WHERE r.user_id=?)",
        (user_id,),
    )
    delete(
        "flash_forecasts",
        "report_id IN (SELECT id FROM research_commissions WHERE user_id=?)",
        (user_id,),
    )
    delete(
        "sports_ai_forecasts",
        "report_id IN (SELECT id FROM research_commissions WHERE user_id=?)",
        (user_id,),
    )
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
    delete("sports_comments", "user_id=?", (user_id,))
    delete("community_calls", "user_id=?", (user_id,))
    delete("memecoin_calls", "user_id=?", (user_id,))
    delete("sports_picks", "user_id=?", (user_id,))
    delete("user_positions", "user_id=?", (user_id,))
    delete("watches", "user_id=?", (user_id,))
    return deleted


def delete_user_content(user_id: str) -> dict[str, Any]:

    with connection() as database:
        tables = _tables(database)
        if "users" not in tables:
            return {"deleted": False, "items_deleted": 0}
        exists = database.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
        if not exists:
            return {"deleted": False, "items_deleted": 0}
        deleted = _delete_user_content_rows(database, tables, user_id)
    return {"deleted": True, "items_deleted": deleted}


def delete_user_data(user_id: str) -> dict[str, Any]:

    with connection() as database:
        tables = _tables(database)
        if "users" not in tables:
            return {"deleted": False}
        exists = database.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
        if not exists:
            return {"deleted": False}

        _delete_user_content_rows(database, tables, user_id)

        def delete(table: str, where: str, parameters: tuple[Any, ...]) -> None:
            if table in tables:
                database.execute(f"DELETE FROM {table} WHERE {where}", parameters)

        delete("comment_avatars", "user_id=?", (user_id,))
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
        delete("flash_transactions", "user_id=?", (user_id,))
        delete("flash_wallets", "user_id=?", (user_id,))
        delete("sessions", "user_id=?", (user_id,))
        delete("auth_challenges", "user_id=?", (user_id,))
        delete("passkeys", "user_id=?", (user_id,))
        database.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"deleted": True}
