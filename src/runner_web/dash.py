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

import os
import secrets
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
DASH_COMMENT_MODEL = "dash-cheetah"


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


DAILY_CALL_LIMIT = max(0, int(os.getenv("DASH_DAILY_CALL_LIMIT", "3")))
DAILY_COMMENT_LIMIT = max(0, int(os.getenv("DASH_DAILY_COMMENT_LIMIT", "5")))


def _acted_today(database: Any, kinds: tuple[str, ...], now: datetime) -> int:
    day_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC).isoformat()
    placeholders = ",".join("?" for _ in kinds)
    return int(
        database.execute(
            f"SELECT COUNT(*) FROM flash_transactions "
            f"WHERE user_id=? AND kind IN ({placeholders}) AND created_at>=?",
            (DASH_USER_ID, *kinds, day_start),
        ).fetchone()[0]
    )


def dash_budget(at: datetime | None = None) -> dict[str, Any]:

    """What Dash can still afford and still allowed to do today.

    The wallet alone would let him spend the whole allowance on the first thing
    anybody asked for. These caps spread it across the day so being asked the
    same question ten times does not empty him.
    """

    now = at or datetime.now(UTC)
    wallet = dash_wallet(at=now)
    with connection() as database:
        comments = _acted_today(database, ("comment_generation",), now)
        calls = database.execute(
            "SELECT COUNT(*) FROM community_calls WHERE user_id=? AND entry_at>=?",
            (
                DASH_USER_ID,
                datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC).isoformat(),
            ),
        ).fetchone()[0]
    return {
        "balance": wallet["balance"],
        "comments_today": comments,
        "comments_left": max(0, DAILY_COMMENT_LIMIT - comments),
        "calls_today": int(calls),
        "calls_left": max(0, DAILY_CALL_LIMIT - int(calls)),
        "can_comment": wallet["balance"] >= 10 and comments < DAILY_COMMENT_LIMIT,
        "can_call": int(calls) < DAILY_CALL_LIMIT,
    }


def dash_make_call(ticker: str, at: datetime | None = None) -> dict[str, Any]:

    """Open a public Call as Dash, on the same terms as anyone else.

    It goes through the shared mark so he is stamped at the price the site would
    show, and it settles and scores through the ordinary path. His Calls count on
    the caller board because he earns Flash from them like a person does, which
    is the whole reason the record is worth anything.
    """

    from runner_web.calls import active_call_for_user, create_call
    from runner_web.main import _current_call_mark

    now = at or datetime.now(UTC)
    symbol = str(ticker).strip().upper().lstrip("$")
    budget = dash_budget(at=now)
    if not budget["can_call"]:
        return {"ok": False, "reason": "out_of_calls_today", "budget": budget}
    ensure_dash_account()
    if active_call_for_user(DASH_USER_ID, symbol):
        return {"ok": False, "reason": "already_open", "ticker": symbol}
    try:
        mark = _current_call_mark(symbol)
    except Exception as exc:
        return {"ok": False, "reason": "no_fresh_price", "detail": type(exc).__name__}
    call = create_call(
        DASH_USER_ID, symbol, entry_price=mark["price"], entry_at=mark["observed_at"]
    )
    if not call:
        return {"ok": False, "reason": "not_created", "ticker": symbol}
    return {
        "ok": True,
        "ticker": symbol,
        "entry_price": mark["price"],
        "entry_at": mark["observed_at"],
        "price_age_seconds": mark.get("age_seconds"),
        "public_id": call.get("public_id"),
    }


def dash_close_call(ticker: str, at: datetime | None = None) -> dict[str, Any]:

    """Close Dash's open Call on a ticker at the current mark."""

    from runner_web.calls import active_call_for_user, close_call
    from runner_web.main import _current_call_mark

    symbol = str(ticker).strip().upper().lstrip("$")
    ensure_dash_account()
    open_call = active_call_for_user(DASH_USER_ID, symbol)
    if not open_call:
        return {"ok": False, "reason": "nothing_open", "ticker": symbol}
    try:
        mark = _current_call_mark(symbol)
    except Exception as exc:
        return {"ok": False, "reason": "no_fresh_price", "detail": type(exc).__name__}
    closed = close_call(
        DASH_USER_ID,
        str(open_call["public_id"]),
        exit_price=mark["price"],
        exit_at=mark["observed_at"],
    )
    if not closed:
        return {"ok": False, "reason": "already_closed", "ticker": symbol}
    return {
        "ok": True,
        "ticker": symbol,
        "exit_price": mark["price"],
        "return_pct": closed.get("return_pct"),
        "flash_reward": closed.get("flash_reward"),
    }


def dash_open_calls() -> list[dict[str, Any]]:

    """Dash's own open positions, so he can talk about what he is holding."""

    from runner_web.calls import caller_call_rows, calls_from_rows

    rows = [row for row in caller_call_rows(DASH_HANDLE) if row["status"] == "active"]
    return [
        {
            "ticker": call.get("ticker"),
            "entry_price": call.get("entry_price"),
            "entry_at": call.get("entry_at"),
            "return_pct": call.get("return_pct"),
        }
        for call in calls_from_rows(rows)
    ]


def dash_comment(ticker: str, body: str, at: datetime | None = None) -> dict[str, Any]:

    """Post a public comment on a ticker as Dash, paid for out of his allowance.

    It is written into the same table a person's avatar comment goes to, with the
    same AI-avatar authorship label, so a reader can tell who is speaking and the
    comment carries the usual disclosure the rest of the product applies.
    """

    from runner_web.flash_wallet import COMMENT_COST, InsufficientFlashError, spend_flash

    now = at or datetime.now(UTC)
    symbol = str(ticker).strip().upper().lstrip("$")
    text = " ".join(str(body).split())[:240]
    if not symbol or not text:
        return {"ok": False, "reason": "nothing_to_say"}
    budget = dash_budget(at=now)
    if budget["comments_left"] <= 0:
        return {"ok": False, "reason": "out_of_comments_today", "budget": budget}
    ensure_dash_account()
    comment_id = secrets.token_urlsafe(10)
    try:
        with connection() as database:
            spend_flash(
                database,
                DASH_USER_ID,
                COMMENT_COST,
                kind="comment_generation",
                reference_id=comment_id,
            )
            database.execute(
                """
                INSERT INTO ticker_comments(
                    id,ticker,subject_kind,subject_key,user_id,body,status,created_at,
                    source,generation_model
                ) VALUES(?,?,'stock',?,?,?,'public',?,'ai_avatar',?)
                """,
                (
                    comment_id,
                    symbol,
                    symbol,
                    DASH_USER_ID,
                    text,
                    now.isoformat(),
                    DASH_COMMENT_MODEL,
                ),
            )
    except InsufficientFlashError:
        return {"ok": False, "reason": "out_of_flash", "budget": budget}
    return {
        "ok": True,
        "ticker": symbol,
        "comment_id": comment_id,
        "body": text,
        "spent": COMMENT_COST,
    }
