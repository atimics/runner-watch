from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from runner_web.db import connection

CLIENT_ERROR_RETENTION_DAYS = 30
CLIENT_ERROR_RECENT_LIMIT = 50
CLIENT_ERROR_WINDOW_HOURS = 24
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def client_ip_hash(value: str, key: bytes) -> str:
    return hashlib.blake2s(str(value or "").encode(), key=key, digest_size=16).hexdigest()


def _clean(value: Any, *, limit: int) -> str:
    text = CONTROL_CHARACTERS.sub(" ", str(value or "")).strip()
    return text[:limit]


def _page_path(value: Any) -> str:
    path = urlsplit(str(value or "")).path
    return _clean(path, limit=300) or "/"


def record_client_error(
    *,
    kind: str,
    message: str,
    source: str = "",
    line: int | None = None,
    column_number: int | None = None,
    stack: str = "",
    page_url: str = "",
    user_agent: str = "",
    release: str = "",
    client_ip: str = "",
    at: datetime | None = None,
) -> str:
    """Store one browser error, collapsing repeats from the same page and hour."""

    observed_at = (at or datetime.now(UTC)).astimezone(UTC)
    kind = _clean(kind, limit=40) or "error"
    message = _clean(message, limit=500) or "Unknown client error"
    source = _clean(source, limit=300)
    stack = _clean(stack, limit=4000)
    path = _page_path(page_url)
    user_agent = _clean(user_agent, limit=300)
    release = _clean(release, limit=60)
    ip_hash = _clean(client_ip, limit=64)
    window = observed_at.replace(minute=0, second=0, microsecond=0).isoformat()
    row_id = hashlib.sha256(
        "|".join([kind, message, source, str(line or ""), path, ip_hash, window]).encode()
    ).hexdigest()[:32]
    timestamp = observed_at.isoformat()
    with connection() as db:
        db.execute(
            """
            INSERT INTO client_errors(
                id,kind,message,source,line,column_number,stack,page_url,user_agent,
                release,client_ip_hash,seen_count,first_seen_at,seen_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                seen_count=client_errors.seen_count+1,
                stack=excluded.stack,
                seen_at=excluded.seen_at
            """,
            (
                row_id,
                kind,
                message,
                source,
                line,
                column_number,
                stack,
                path,
                user_agent,
                release,
                ip_hash,
                1,
                timestamp,
                timestamp,
            ),
        )
    return row_id


def client_error_summary(
    *,
    limit: int = CLIENT_ERROR_RECENT_LIMIT,
    at: datetime | None = None,
) -> dict[str, Any]:
    observed_at = (at or datetime.now(UTC)).astimezone(UTC)
    since = (observed_at - timedelta(hours=CLIENT_ERROR_WINDOW_HOURS)).isoformat()
    with connection() as db:
        totals = db.execute(
            """
            SELECT COUNT(*) AS unique_errors,COALESCE(SUM(seen_count),0) AS reports
            FROM client_errors WHERE seen_at>=?
            """,
            (since,),
        ).fetchone()
        rows = db.execute(
            """
            SELECT kind,message,source,line,page_url,user_agent,release,seen_count,
                   first_seen_at,seen_at
            FROM client_errors WHERE seen_at>=?
            ORDER BY seen_at DESC LIMIT ?
            """,
            (since, max(1, min(int(limit), 200))),
        ).fetchall()
    return {
        "status": "ok",
        "window_hours": CLIENT_ERROR_WINDOW_HOURS,
        "unique_errors": int(totals["unique_errors"] or 0) if totals else 0,
        "reports": int(totals["reports"] or 0) if totals else 0,
        "errors": [dict(row) for row in rows],
    }