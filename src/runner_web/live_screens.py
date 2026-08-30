from __future__ import annotations

from typing import Any
from urllib.parse import quote


def _latest_value(database: Any, query: str) -> str | None:
    row = database.execute(query).fetchone()
    if not row:
        return None
    value = str(row[0] or "").strip()
    return value or None


def public_dynamic_screen_paths(database: Any) -> dict[str, str | None]:
    """Return current public records that make dynamic screens safe to smoke test."""

    ticker = _latest_value(
        database,
        """
        SELECT ticker
        FROM scan_snapshots
        WHERE ticker<>''
        ORDER BY captured_at DESC
        LIMIT 1
        """,
    )
    caller = _latest_value(
        database,
        """
        SELECT ci.handle
        FROM community_calls c
        JOIN caller_identities ci ON ci.id=c.caller_identity_id
        WHERE ci.status='active'
        ORDER BY c.updated_at DESC
        LIMIT 1
        """,
    )
    research = _latest_value(
        database,
        """
        SELECT public_id
        FROM research_commissions
        WHERE status='complete' AND visibility='public'
        ORDER BY COALESCE(published_at,completed_at,created_at) DESC
        LIMIT 1
        """,
    )
    game = _latest_value(
        database,
        """
        SELECT id
        FROM sports_events
        ORDER BY
            CASE WHEN status='in' THEN 0 WHEN status='pre' THEN 1 ELSE 2 END,
            CASE WHEN status IN ('in','pre') THEN start_time END ASC,
            start_time DESC
        LIMIT 1
        """,
    )

    return {
        "ticker": f"/t/{quote(ticker, safe='.-')}" if ticker else None,
        "caller": f"/u/{quote(caller, safe='-')}" if caller else None,
        "research": f"/research/{quote(research, safe='')}" if research else None,
        "sports_game": f"/game/{quote(game, safe=':-')}" if game else None,
    }
