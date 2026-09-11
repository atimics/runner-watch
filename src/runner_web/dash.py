"""Dash's account and what he can see.

Dash reads the room in Telegram, but answering "what is going on in the market"
needs him to see the same board everyone else does, and doing anything about it
needs him to be somebody in the system rather than a voice attached to nothing.

He gets a real account for that reason. The machine already plays the daily
Calls game from its own account so its record is comparable to a human's, and
Dash follows the same pattern: one users row, one caller identity, one comment
avatar, one Flash wallet. Everything he does then runs on the same rails as
everyone else, is paid for out of the same daily allowance, and shows up in the
same places, rather than living in a parallel set of tables nobody audits.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from runner_web.db import connection
from runner_web.flash_wallet import claim_daily_flash
from runner_web.pseudonyms import comment_avatar_profile

DASH_USER_ID = "dash-cheetah"
DASH_HANDLE = "dash"
DASH_DISPLAY_NAME = "Dash"
DASH_AVATAR_NAME = "Swift Amber Cheetah"
DASH_AVATAR_SEED = "d45ha1cheetah7b2e9f04c6183ad5e27"
DASH_AVATAR_ABILITY = "pattern_mapper"


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def dash_avatar() -> dict[str, Any]:

    """Dash's face, fixed rather than rolled.

    Everyone else gets a random avatar on first comment. Dash is a character
    people are meant to recognise, so his seed is pinned and his comments always
    render the same way.
    """

    return comment_avatar_profile(
        DASH_AVATAR_NAME, DASH_AVATAR_SEED, DASH_AVATAR_ABILITY, level=1
    )


def ensure_dash_account(database: Any = None) -> dict[str, Any]:

    """Create or return Dash's account, caller identity and avatar."""

    if database is None:
        with connection() as database:
            return ensure_dash_account(database)
    timestamp = _iso()
    database.execute(
        """
        INSERT INTO users(id,username,display_name,status,created_at)
        VALUES(?,?,?,'active',?) ON CONFLICT DO NOTHING
        """,
        (DASH_USER_ID, DASH_HANDLE, DASH_DISPLAY_NAME, timestamp),
    )
    database.execute(
        """
        INSERT INTO caller_identities(
            id,handle,user_id,status,claim_cost_cents,claimed_at
        ) VALUES(?,?,?,'active',0,?) ON CONFLICT DO NOTHING
        """,
        (DASH_USER_ID, DASH_HANDLE, DASH_USER_ID, timestamp),
    )
    database.execute(
        """
        INSERT INTO comment_avatars(user_id,name,seed,ability_id,level,created_at)
        VALUES(?,?,?,?,1,?) ON CONFLICT DO NOTHING
        """,
        (DASH_USER_ID, DASH_AVATAR_NAME, DASH_AVATAR_SEED, DASH_AVATAR_ABILITY, timestamp),
    )
    return {
        "user_id": DASH_USER_ID,
        "handle": DASH_HANDLE,
        "display_name": DASH_DISPLAY_NAME,
        "avatar": dash_avatar(),
    }


def dash_wallet(at: datetime | None = None) -> dict[str, Any]:

    """Dash's balance, claiming the day's allowance if it is still there.

    He is funded the same way a person is: one hundred a day, no more. That is
    what keeps him from commissioning reports all afternoon, and it means an
    inaccurate Dash runs out while an accurate one pays for himself.
    """

    ensure_dash_account()
    wallet, _claimed = claim_daily_flash(DASH_USER_ID, at=at)
    return wallet


def market_now(at: datetime | None = None) -> dict[str, Any]:

    """What is going on right now, in the terms the board already uses.

    This is assembled from stored scanner output and the frozen session reports
    rather than from anything Dash decides, so when he describes the tape he is
    describing the same thing the site shows.
    """

    from runner_web.main import _public_pulse_data
    from runner_web.market_clock import market_clock
    from runner_web.market_reports import market_reports_overview

    clock = market_clock(at)
    board = _public_pulse_data(limit=20)
    rows = list(board.get("rows") or [])
    changes = [
        float(row["change_pct"]) for row in rows if isinstance(row.get("change_pct"), (int, float))
    ]
    movers = sorted(
        (row for row in rows if isinstance(row.get("change_pct"), (int, float))),
        key=lambda row: float(row["change_pct"]),
        reverse=True,
    )
    overview = market_reports_overview(at, history_limit=0)
    featured = overview.get("featured")
    return {
        "session": clock["label"],
        "session_note": clock["data_note"],
        "board_size": len(rows),
        "green": sum(value > 0 for value in changes),
        "red": sum(value < 0 for value in changes),
        "average_change_pct": round(sum(changes) / len(changes), 2) if changes else None,
        "updated_at": board.get("updated_at"),
        "leaders": [
            {
                "ticker": row.get("ticker"),
                "company": row.get("company"),
                "change_pct": row.get("change_pct"),
                "relative_volume": row.get("relative_volume"),
                "price": row.get("price"),
                "pulse_label": row.get("pulse_label"),
            }
            for row in movers[:5]
        ],
        "laggards": [
            {"ticker": row.get("ticker"), "change_pct": row.get("change_pct")}
            for row in movers[-3:][::-1]
            if float(row.get("change_pct") or 0) < 0
        ],
        "latest_report": (
            {
                "label": featured.get("label"),
                "day": featured.get("report_day"),
                "headline": featured.get("headline"),
                "flash_take": (featured.get("analysis") or {}).get("headline"),
                "record": (featured.get("forecast_record") or {}).get("label"),
            }
            if featured
            else None
        ),
    }


def sector_now(sector: str | None = None, at: datetime | None = None) -> dict[str, Any]:

    """The board grouped by sector, or one sector on its own.

    Sectors come from the SIC code SEC publishes per filer, backfilled a little
    at a time. Names that have not been looked up yet are reported as
    unclassified rather than quietly folded into a group they may not belong to.
    """

    from runner_web.main import _public_pulse_data
    from runner_web.sectors import sector_board

    rows = list((_public_pulse_data(limit=50)).get("rows") or [])
    with connection() as database:
        board = sector_board(database, rows)
    if sector:
        wanted = str(sector).strip().lower()
        matched = [
            group
            for group in board
            if wanted in str(group["sector"]).lower()
        ]
        return {
            "asked_for": sector,
            "matched": bool(matched),
            "sectors": matched,
            "available": [group["sector"] for group in board],
        }
    return {"asked_for": None, "matched": bool(board), "sectors": board[:8]}
