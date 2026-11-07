"""Dash's trade ideas and the operator's bounded order approvals."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from solders.pubkey import Pubkey

from runner_trading import ledger
from runner_trading.config import USDC, Policy, account_id, age, stamp
from runner_trading.providers import Jupiter, ProviderError, SolanaRPC, positive_int
from runner_trading.strategy import entry_reasons
from runner_web.db import connection

ACCOUNT = account_id("live")
REVIEW_SECONDS = 120


def policy() -> Policy:
    return Policy(**json.loads(ledger.account(ACCOUNT)["policy_json"]))


def board_row(mint: str) -> dict:
    with connection() as db:
        saved = db.execute(
            "SELECT value FROM worker_state WHERE key='memecoins_snapshot'"
        ).fetchone()
    rows = json.loads(saved["value"]).get("rows", []) if saved else []
    return next((r for r in rows if r.get("token_address") == mint), {})


def get(identifier: str) -> dict:
    with connection() as db:
        row = db.execute(
            "SELECT * FROM trading_proposals WHERE id=? AND account_id=?", (identifier, ACCOUNT)
        ).fetchone()
    if row is None:
        raise ValueError("Choose an existing trade proposal")
    return dict(row)


def listing() -> list[dict]:
    with connection() as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT p.id,p.mint,p.side,p.input_raw,p.thesis,p.invalidation,p.created_at,"
                "p.status,p.order_id,o.status AS order_status FROM trading_proposals p "
                "LEFT JOIN trading_orders o ON o.id=p.order_id WHERE p.account_id=? "
                "ORDER BY p.created_at DESC LIMIT 50",
                (ACCOUNT,),
            ).fetchall()
        ]


def create(
    *,
    source: str,
    mint: str,
    side: str,
    amount: int | None,
    thesis: str,
    invalidation: str,
    at: datetime | None = None,
) -> dict:
    now = at or datetime.now(UTC)
    Pubkey.from_string(mint)
    if side not in {"buy", "sell"} or mint == USDC:
        raise ValueError("Choose a Solana token to buy or sell for USDC")
    if not thesis.strip() or not invalidation.strip():
        raise ValueError("Explain the trade and what would change the view")
    identifier = ledger.key("dash-trade", source)
    # The update identity makes retries return the first saved idea.
    with connection() as db:
        existing = db.execute(
            "SELECT id FROM trading_proposals WHERE id=?", (identifier,)
        ).fetchone()
    if existing:
        return get(identifier)
    position_id = None
    acc = ledger.account(ACCOUNT)
    if side == "buy":
        amount = positive_int(amount)
        if amount > acc["cash_usdc"] - acc["reserved_usdc"]:
            raise ValueError("Choose an amount within the funded USDC balance")
    else:
        positions = ledger.rows("trading_positions", ACCOUNT, active=True)
        pos = next((p for p in positions if p["mint"] == mint), None)
        if pos is None:
            raise ValueError("Choose a token held in Dash's live account")
        position_id, amount = pos["id"], int(pos["quantity_raw"])
    with connection() as db:
        count = db.execute(
            "SELECT COUNT(*) AS n FROM trading_proposals WHERE account_id=? AND status='proposed'",
            (ACCOUNT,),
        ).fetchone()["n"]
        if count >= 20:
            raise ValueError("Review the open proposals before adding another")
        db.execute(
            "INSERT INTO trading_proposals(id,account_id,mint,side,input_raw,position_id,"
            "thesis,invalidation,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (
                identifier,
                ACCOUNT,
                mint,
                side,
                str(amount),
                position_id,
                thesis.strip()[:2000],
                invalidation.strip()[:1000],
                stamp(now),
            ),
        )
    return get(identifier)


def check(proposal: dict, acc: dict, rules: Policy, now: datetime) -> dict:
    amount = positive_int(proposal["input_raw"])
    if acc["wallet"] != os.getenv("TRADING_WALLET"):
        raise ValueError("Use the wallet saved for this account")
    if not 0 <= age(acc["heartbeat_at"], now) <= rules.cycle_seconds * 4:
        raise ValueError("Wait for a fresh live wallet balance")
    health = json.loads(acc["health_json"])
    if health.get("errors") or health.get("uncertain_orders"):
        raise ValueError("Resolve the live worker's account checks")
    if proposal["side"] == "buy":
        if acc["paused"]:
            raise ValueError("Resume entries before approving a purchase")
        if amount > acc["cash_usdc"] - acc["reserved_usdc"]:
            raise ValueError("Choose an amount within the funded USDC balance")
        row = board_row(proposal["mint"])
        reasons = entry_reasons(row, rules, now, amount=amount)
        if reasons:
            raise ValueError("; ".join(reasons))
        return row
    pos = next(
        (
            p
            for p in ledger.rows("trading_positions", ACCOUNT, active=True)
            if p["id"] == proposal["position_id"]
        ),
        None,
    )
    if pos is None or int(pos["quantity_raw"]) != amount or pos["mint"] != proposal["mint"]:
        raise ValueError("Refresh the proposal for the current position")
    return {}


def preview(identifier: str, *, provider=None, rpc=None, clock=None) -> dict:
    clock = clock or (lambda: datetime.now(UTC))
    proposal, rules = get(identifier), policy()
    if proposal["status"] != "proposed":
        raise ValueError("Choose an open proposal")
    acc = ledger.account(ACCOUNT)
    check(proposal, acc, rules, clock())
    wallet = str(Pubkey.from_string(os.environ.get("TRADING_WALLET", "")))
    provider, rpc = provider or Jupiter(rules), rpc or SolanaRPC()
    decimals = rpc("getTokenSupply", [proposal["mint"], {"commitment": "finalized"}])["value"][
        "decimals"
    ]
    if type(decimals) is not int or not 0 <= decimals <= 18:
        raise ValueError("Read the token's unit size")
    buying = proposal["side"] == "buy"
    quote = provider.quote(
        USDC if buying else proposal["mint"],
        proposal["mint"] if buying else USDC,
        int(proposal["input_raw"]),
        taker=wallet,
    )
    quote.require_fresh(rules, clock())
    review = {
        **quote.receipt(),
        "wallet": wallet,
        "policy_hash": rules.fingerprint,
        "max_fee_lamports": rules.max_fee_lamports,
        "input_amount": format(Decimal(quote.input_raw).scaleb(-6 if buying else -decimals), "f"),
        "minimum_amount": format(
            Decimal(quote.minimum_raw).scaleb(-decimals if buying else -6), "f"
        ),
        "balance_share_percent": round(
            100 * quote.input_raw / (acc["cash_usdc"] - acc["reserved_usdc"]), 2
        )
        if buying
        else 100,
        "thesis": proposal["thesis"],
        "invalidation": proposal["invalidation"],
        "side": proposal["side"],
        "mint": proposal["mint"],
        "expires_at": stamp(clock() + timedelta(seconds=REVIEW_SECONDS)),
    }
    token = secrets.token_hex(24)
    with connection() as db:
        changed = db.execute(
            "UPDATE trading_proposals SET review_token=?,review_json=?,review_until=? "
            "WHERE id=? AND status='proposed'",
            (token, ledger.encode(review), review["expires_at"], identifier),
        )
        if changed.rowcount != 1:
            raise ValueError("Refresh the proposal")
    return {**review, "review_token": token, "proposal_id": identifier}


def approve(
    identifier: str, review_token: str, operator: str, *, at: datetime | None = None
) -> str:
    now = at or datetime.now(UTC)
    token = ledger.acquire(ACCOUNT, now)
    if token is None:
        raise ValueError("The wallet is being checked. Try approval again shortly")
    try:
        with ledger.locked(ACCOUNT, token, now) as db:
            p = dict(
                db.execute(
                    "SELECT * FROM trading_proposals WHERE id=? AND account_id=?",
                    (identifier, ACCOUNT),
                ).fetchone()
                or {}
            )
            if (
                not p
                or not review_token
                or not secrets.compare_digest(p.get("review_token") or "", review_token)
            ):
                raise ValueError("Review the current quote before approval")
            if p["status"] == "approved":
                return p["order_id"]
            if p["status"] != "proposed" or age(p["review_until"], now) >= 0:
                raise ValueError("Refresh the quote before approval")
            acc = dict(
                db.execute("SELECT * FROM trading_accounts WHERE id=?", (ACCOUNT,)).fetchone()
            )
            rules, review = Policy(**json.loads(acc["policy_json"])), json.loads(p["review_json"])
            if (
                review["wallet"] != os.getenv("TRADING_WALLET")
                or review["policy_hash"] != rules.fingerprint
            ):
                raise ValueError("Review the current wallet and limits")
            row = check(p, acc, rules, now)
            pending = db.execute(
                "SELECT COUNT(*) AS n FROM trading_orders WHERE account_id=? "
                "AND status IN ('queued','prepared','submitted','checking')",
                (ACCOUNT,),
            ).fetchone()["n"]
            if (
                acc["sol_lamports"]
                < rules.min_sol_lamports + (pending + 1) * rules.max_fee_lamports
            ):
                raise ValueError("Add SOL for the fee reserve")
            duplicate = db.execute(
                "SELECT 1 FROM trading_orders WHERE account_id=? AND mint=? "
                "AND status IN ('queued','prepared','submitted','checking')",
                (ACCOUNT, p["mint"]),
            ).fetchone()
            if duplicate:
                raise ValueError("Wait for the current order for this token")
            buying, amount = p["side"] == "buy", int(p["input_raw"])
            signal_id = identifier if buying else None
            if buying:
                held = db.execute(
                    "SELECT mint FROM trading_positions WHERE account_id=? AND status='open'",
                    (ACCOUNT,),
                ).fetchall()
                buys = db.execute(
                    "SELECT COUNT(*) AS n FROM trading_orders WHERE account_id=? AND side='buy' "
                    "AND status IN ('queued','prepared','submitted','checking')",
                    (ACCOUNT,),
                ).fetchone()["n"]
                if len(held) + buys >= rules.max_positions or any(
                    h["mint"] == p["mint"] for h in held
                ):
                    raise ValueError("Choose an entry within the open-position limit")
                db.execute(
                    "INSERT INTO "
                    "trading_signals(id,account_id,mint,coin_id,observed_at,state,reason,"
                    "evidence_json,outcome_due_at) "
                    "VALUES(?,?,?,?,?,'ordered',?,?,?) ON CONFLICT DO NOTHING",
                    (
                        identifier,
                        ACCOUNT,
                        p["mint"],
                        row["id"],
                        stamp(now),
                        p["thesis"],
                        ledger.encode(row),
                        stamp(now + timedelta(hours=6)),
                    ),
                )
                signal_id = db.execute(
                    "SELECT id FROM trading_signals "
                    "WHERE account_id=? AND mint=? AND observed_at=?",
                    (ACCOUNT, p["mint"], stamp(now)),
                ).fetchone()["id"]
                if db.execute(
                    "SELECT 1 FROM trading_orders WHERE signal_id=? AND side='buy'", (signal_id,)
                ).fetchone():
                    raise ValueError("Wait for a new entry review")
                db.execute(
                    "UPDATE trading_signals SET state='ordered',reason=? WHERE id=?",
                    (p["thesis"], signal_id),
                )
            db.execute(
                "INSERT INTO "
                "trading_orders(id,account_id,signal_id,position_id,mint,side,status,input_raw,"
                "reserved_usdc,reason,created_at,updated_at,ready_at) "
                "VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?,?)",
                (
                    identifier,
                    ACCOUNT,
                    signal_id,
                    p["position_id"],
                    p["mint"],
                    p["side"],
                    p["input_raw"],
                    amount if buying else 0,
                    p["thesis"],
                    stamp(now),
                    stamp(now),
                    stamp(now),
                ),
            )
            if buying:
                db.execute(
                    "UPDATE trading_accounts SET reserved_usdc=reserved_usdc+? WHERE id=?",
                    (amount, ACCOUNT),
                )
            changed = db.execute(
                "UPDATE trading_proposals SET "
                "status='approved',approved_by=?,approved_at=?,order_id=? "
                "WHERE id=? AND status='proposed' AND review_token=?",
                (operator, stamp(now), identifier, identifier, review_token),
            )
            if changed.rowcount != 1:
                raise ValueError("The proposal changed. Review it again")
            ledger.event(
                db,
                ACCOUNT,
                "trade_approved",
                {"proposal_id": identifier, "operator_id": operator},
                now,
            )
        return identifier
    finally:
        ledger.release(ACCOUNT, token)


def reject(identifier: str, operator: str) -> None:
    with connection() as db:
        changed = db.execute(
            "UPDATE trading_proposals SET status='rejected',review_token=NULL "
            "WHERE id=? AND account_id=? AND status='proposed'",
            (identifier, ACCOUNT),
        )
        if changed.rowcount != 1:
            raise ValueError("Choose an open proposal")
        ledger.event(
            db,
            ACCOUNT,
            "trade_rejected",
            {"proposal_id": identifier, "operator_id": operator},
            datetime.now(UTC),
        )


def require_approval(order: dict, quote, wallet: str, rules: Policy, now: datetime) -> None:
    with connection() as db:
        p = db.execute(
            "SELECT * FROM trading_proposals WHERE order_id=? AND "
            "account_id=? AND status='approved'",
            (order["id"], order["account_id"]),
        ).fetchone()
    if p is None:
        raise ProviderError("An operator-approved quote is required")
    review = json.loads(p["review_json"])
    if (
        age(p["review_until"], now) >= 0
        or review["wallet"] != wallet
        or review["policy_hash"] != rules.fingerprint
        or p["side"] != order["side"]
        or p["mint"] != order["mint"]
        or p["input_raw"] != order["input_raw"]
        or quote.input_mint != review["input_mint"]
        or quote.output_mint != review["output_mint"]
        or quote.input_raw != int(review["input_raw"])
        or quote.minimum_raw < int(review["minimum_raw"])
        or quote.fee_lamports > review["max_fee_lamports"]
    ):
        raise ProviderError("A fresh approval is needed for the current trade terms")
