from __future__ import annotations

import json
from datetime import UTC, datetime

from runner_trading.config import account_id, age, stamp
from runner_web.db import connection


def decision_coverage(db, identifier: str, at: datetime) -> dict:
    outcome_state = (
        "outcome_json::jsonb->>'state'"
        if db.backend == "postgres"
        else "json_extract(outcome_json,'$.state')"
    )
    cohort = dict(
        db.execute(
            "SELECT COUNT(*) AS total,COUNT(DISTINCT mint) AS distinct_tokens,"
            "SUM(CASE WHEN state='skipped' THEN 1 ELSE 0 END) AS skipped,"
            "SUM(CASE WHEN state='entered' THEN 1 ELSE 0 END) AS entries_filled,"
            "SUM(CASE WHEN state='entry_failed' THEN 1 ELSE 0 END) AS entries_failed,"
            "SUM(CASE WHEN baseline_units IS NOT NULL THEN 1 ELSE 0 END) AS entry_quoted,"
            "SUM(CASE WHEN outcome_at IS NOT NULL THEN 1 ELSE 0 END) AS outcome_processed,"
            "SUM(CASE WHEN outcome_at IS NOT NULL AND outcome_state='quoted' "
            "THEN 1 ELSE 0 END) AS outcome_priced,"
            "SUM(CASE WHEN outcome_at IS NOT NULL AND outcome_state='unavailable' "
            "THEN 1 ELSE 0 END) AS outcome_unavailable,"
            "SUM(CASE WHEN outcome_at IS NULL THEN 1 ELSE 0 END) AS outcome_pending,"
            "SUM(CASE WHEN outcome_due_at<=? THEN 1 ELSE 0 END) AS outcome_due,"
            "SUM(CASE WHEN outcome_due_at<=? AND outcome_at IS NULL "
            "THEN 1 ELSE 0 END) AS outcome_overdue,"
            "SUM(CASE WHEN outcome_due_at<=? AND outcome_at IS NOT NULL "
            "AND outcome_state='quoted' THEN 1 ELSE 0 END) AS priced_due "
            "FROM (SELECT mint,state,baseline_units,outcome_at,outcome_due_at,"
            f"{outcome_state} AS outcome_state FROM trading_signals WHERE account_id=?) decisions",
            (stamp(at), stamp(at), stamp(at), identifier),
        ).fetchone()
    )
    cohort = {key: int(value or 0) for key, value in cohort.items()}
    priced_due = cohort.pop("priced_due")
    cohort["outcome_coverage_percent"] = (
        round(100 * priced_due / cohort["outcome_due"], 1) if cohort["outcome_due"] else None
    )
    cohort["outcome_unclassified"] = (
        cohort["outcome_processed"] - cohort["outcome_priced"] - cohort["outcome_unavailable"]
    )
    # Preserve the existing API field for clients; it counts attempts, not returns.
    cohort["resolved"] = cohort["outcome_processed"]
    cohort["closed_trades"] = db.execute(
        "SELECT COUNT(*) FROM trading_positions WHERE account_id=? AND status='closed'",
        (identifier,),
    ).fetchone()[0]
    return cohort


def snapshot(mode: str, *, at: datetime | None = None) -> dict:
    now = at or datetime.now(UTC)
    identifier = account_id(mode)
    with connection() as db:
        account = db.execute("SELECT * FROM trading_accounts WHERE id=?", (identifier,)).fetchone()
        if account is None:
            return {
                "state": "preparing",
                "mode": mode,
                "positions": [],
                "orders": [],
                "signals": [],
                "events": [],
                "cohort": {"total": 0},
            }
        policy = json.loads(account["policy_json"])
        positions = [
            dict(p)
            for p in db.execute(
                "SELECT p.*,s.evidence_json FROM trading_positions p JOIN trading_signals s "
                "ON s.id=p.signal_id WHERE p.account_id=? "
                "ORDER BY CASE WHEN p.status='open' THEN 0 ELSE 1 END,p.opened_at DESC LIMIT 100",
                (identifier,),
            ).fetchall()
        ]
        open_totals = db.execute(
            "SELECT COUNT(*) AS n,COALESCE(SUM(mark_usdc),0) AS value FROM trading_positions "
            "WHERE account_id=? AND status='open'",
            (identifier,),
        ).fetchone()
        stale = db.execute(
            "SELECT mark_at,mark_usdc FROM trading_positions WHERE account_id=? AND status='open'",
            (identifier,),
        ).fetchall()
        complete = all(
            p["mark_usdc"] is not None
            and 0 <= age(p["mark_at"], now) <= policy["cycle_seconds"] * 3
            for p in stale
        )
        orders = [
            dict(o)
            for o in db.execute(
                "SELECT id,mint,side,status,input_raw,reason,created_at,updated_at,"
                "attempts,result_json "
                "FROM trading_orders WHERE account_id=? ORDER BY created_at DESC LIMIT 50",
                (identifier,),
            ).fetchall()
        ]
        signals = [
            dict(s)
            for s in db.execute(
                "SELECT id,mint,coin_id,observed_at,state,reason,outcome_at,outcome_json "
                "FROM trading_signals WHERE account_id=? ORDER BY observed_at DESC LIMIT 50",
                (identifier,),
            ).fetchall()
        ]
        cohort = decision_coverage(db, identifier, now)
        events = [
            dict(e)
            for e in db.execute(
                "SELECT kind,detail_json,recorded_at FROM trading_events WHERE account_id=? "
                "ORDER BY recorded_at DESC LIMIT 20",
                (identifier,),
            ).fetchall()
        ]
    for pos in positions:
        evidence = json.loads(pos.pop("evidence_json"))
        pos["symbol"] = evidence.get("symbol") or pos["mint"][:8]
        pos["name"] = evidence.get("name") or pos["symbol"]
        pos["stale"] = pos["status"] == "open" and (
            pos["mark_usdc"] is None or age(pos["mark_at"], now) > policy["cycle_seconds"] * 3
        )
        pos.pop("mark_json", None)
    for order in orders:
        result = json.loads(order.pop("result_json") or "{}")
        order["detail"] = result.get("reason") or order["reason"]
    for signal in signals:
        signal["outcome"] = json.loads(signal.pop("outcome_json") or "null")
    for event in events:
        event["detail"] = json.loads(event.pop("detail_json"))
        event["detail"].pop("operator_id", None)
    health = json.loads(account["health_json"])
    healthy = 0 <= age(account["heartbeat_at"], now) <= policy["cycle_seconds"] * 4
    return {
        "mode": mode,
        "state": health.get("state", "preparing") if healthy else "stale",
        "paused": bool(account["paused"]),
        "pause_reason": account["pause_reason"],
        "cash_usdc": account["cash_usdc"],
        "reserved_usdc": account["reserved_usdc"],
        "available_usdc": account["cash_usdc"] - account["reserved_usdc"],
        "sol_lamports": account["sol_lamports"],
        "gas_usdc": account["gas_usdc"],
        "realized_usdc": account["realized_usdc"],
        "valuation_complete": complete,
        "equity_usdc": account["cash_usdc"] + int(open_totals["value"]) - account["gas_usdc"]
        if complete
        else None,
        "open_positions": open_totals["n"],
        "policy": policy,
        "policy_hash": account["policy_hash"],
        "heartbeat_at": account["heartbeat_at"],
        "health": health,
        "positions": positions,
        "orders": orders,
        "signals": signals,
        "cohort": cohort,
        "events": events,
    }
