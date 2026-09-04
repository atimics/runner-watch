from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from runner_web.db import connection

DAILY_CLAIM_AMOUNT = 100
REPORT_COST = 100
COMMENT_COST = 10
PUBLISH_REPORT_REWARD = 50
WINNING_CALL_REWARD = 25
SPORTS_CALL_REWARD_CAP = 50
CALL_CLOSE_REWARD_MULTIPLIER = 10
REPORT_EXCLUSIVE_HOURS = 1


class InsufficientFlashError(ValueError):
    def __init__(self, balance: int, cost: int) -> None:
        self.balance = balance
        self.cost = cost
        super().__init__(f"This action costs {cost} Flash. Your balance is {balance}.")


def runner_call_reward(return_pct: float | int | None) -> int:


    if return_pct is None or float(return_pct) <= 0:
        return 0
    reward = Decimal(str(return_pct)) * CALL_CLOSE_REWARD_MULTIPLIER
    return int(reward.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def sports_call_reward(american_odds: int | float | None) -> int:


    if american_odds is None:
        return 0
    odds = int(american_odds)
    if odds == 0:
        return 0
    profit_units = odds / 100 if odds > 0 else 100 / abs(odds)
    return max(1, min(SPORTS_CALL_REWARD_CAP, round(WINNING_CALL_REWARD * profit_units)))


def _timestamp(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    return current.astimezone(UTC) if current.tzinfo else current.replace(tzinfo=UTC)


def _ensure_wallet(database: Any, user_id: str, created_at: str) -> None:
    database.execute(
        """
        INSERT INTO flash_wallets(user_id,balance,created_at,updated_at)
        VALUES(?,0,?,?) ON CONFLICT DO NOTHING
        """,
        (user_id, created_at, created_at),
    )


def _wallet_row(database: Any, user_id: str) -> Any:
    row = database.execute(
        """
        SELECT user_id,balance,last_claim_on,created_at,updated_at
        FROM flash_wallets WHERE user_id=?
        """,
        (user_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("Flash wallet was not created")
    return row


def _wallet_payload(row: Any, current: datetime) -> dict[str, Any]:
    today = current.date().isoformat()
    next_claim = datetime.combine(
        current.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=UTC,
    )
    claimed_today = str(row["last_claim_on"] or "") == today
    return {
        "balance": int(row["balance"]),
        "claim_day": today,
        "daily_claim": DAILY_CLAIM_AMOUNT,
        "claimed_today": claimed_today,
        "can_claim": not claimed_today,
        "next_claim_at": next_claim.isoformat(),
        "report_cost": REPORT_COST,
        "comment_cost": COMMENT_COST,
        "publish_reward": PUBLISH_REPORT_REWARD,
        "winning_call_reward": WINNING_CALL_REWARD,
        "sports_call_reward_cap": SPORTS_CALL_REWARD_CAP,
        "call_close_reward_multiplier": CALL_CLOSE_REWARD_MULTIPLIER,
    }


def wallet_for_user(user_id: str, *, at: datetime | None = None) -> dict[str, Any]:
    current = _timestamp(at)
    timestamp = current.isoformat()
    with connection() as database:
        _ensure_wallet(database, user_id, timestamp)
        row = _wallet_row(database, user_id)
    return _wallet_payload(row, current)


def claim_daily_flash(
    user_id: str,
    *,
    at: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    current = _timestamp(at)
    timestamp = current.isoformat()
    claim_on = current.date().isoformat()
    with connection() as database:
        _ensure_wallet(database, user_id, timestamp)
        inserted = database.execute(
            """
            INSERT INTO flash_transactions(id,user_id,amount,kind,reference_id,created_at)
            VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                user_id,
                DAILY_CLAIM_AMOUNT,
                "daily_claim",
                claim_on,
                timestamp,
            ),
        )
        claimed = inserted.rowcount > 0
        if claimed:
            database.execute(
                """
                UPDATE flash_wallets
                SET balance=balance+?,last_claim_on=?,updated_at=?
                WHERE user_id=?
                """,
                (DAILY_CLAIM_AMOUNT, claim_on, timestamp, user_id),
            )
        row = _wallet_row(database, user_id)
    return _wallet_payload(row, current), claimed


def spend_flash(
    database: Any,
    user_id: str,
    amount: int,
    *,
    kind: str,
    reference_id: str,
    at: datetime | None = None,
) -> tuple[int, bool]:
    if amount <= 0:
        raise ValueError("Flash spend must be positive")
    current = _timestamp(at)
    timestamp = current.isoformat()
    _ensure_wallet(database, user_id, timestamp)
    inserted = database.execute(
        """
        INSERT INTO flash_transactions(id,user_id,amount,kind,reference_id,created_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING
        """,
        (str(uuid.uuid4()), user_id, -amount, kind, reference_id, timestamp),
    )
    if inserted.rowcount == 0:
        return int(_wallet_row(database, user_id)["balance"]), False
    updated = database.execute(
        """
        UPDATE flash_wallets SET balance=balance-?,updated_at=?
        WHERE user_id=? AND balance>=?
        """,
        (amount, timestamp, user_id, amount),
    )
    if updated.rowcount == 0:
        balance = int(_wallet_row(database, user_id)["balance"])
        raise InsufficientFlashError(balance, amount)
    return int(_wallet_row(database, user_id)["balance"]), True


def credit_flash(
    database: Any,
    user_id: str,
    amount: int,
    *,
    kind: str,
    reference_id: str,
    at: datetime | None = None,
) -> tuple[int, bool]:
    if amount <= 0:
        raise ValueError("Flash credit must be positive")
    current = _timestamp(at)
    timestamp = current.isoformat()
    _ensure_wallet(database, user_id, timestamp)
    inserted = database.execute(
        """
        INSERT INTO flash_transactions(id,user_id,amount,kind,reference_id,created_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING
        """,
        (str(uuid.uuid4()), user_id, amount, kind, reference_id, timestamp),
    )
    if inserted.rowcount:
        database.execute(
            "UPDATE flash_wallets SET balance=balance+?,updated_at=? WHERE user_id=?",
            (amount, timestamp, user_id),
        )
    return int(_wallet_row(database, user_id)["balance"]), inserted.rowcount > 0


def recent_transactions(user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    with connection() as database:
        rows = database.execute(
            """
            SELECT amount,kind,reference_id,created_at
            FROM flash_transactions WHERE user_id=?
            ORDER BY created_at DESC,id DESC LIMIT ?
            """,
            (user_id, min(50, max(1, limit))),
        ).fetchall()
    return [dict(row) for row in rows]
