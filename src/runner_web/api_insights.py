"""Bounded summaries of saved evidence for API readers."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.memecoin_chain_parser import SOL, USDC

VERSION = "api-insights-v1"
CHECK_LABELS = {
    "mint_authority": "Mint authority",
    "freeze_authority": "Freeze authority",
    "token_features": "Token features",
    "pool": "Pool liquidity",
    "age": "Pool age",
    "record": "Launch and trading record",
    "holders": "Holder concentration",
    "holder_count": "Holder count",
    "liquidity_lock": "Liquidity lock",
}


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def moment(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def freshness(value: Any, *, at: datetime, max_age: int = 900) -> dict[str, Any]:
    observed = moment(value)
    age = (at - observed).total_seconds() if observed else None
    valid = age is not None and age >= -60
    return {
        "status": "unknown" if not valid else "stale" if age >= max_age else "fresh",
        "as_of": observed.isoformat() if observed else None,
        "age_seconds": max(0, int(age)) if valid else None,
        "max_age_seconds": max_age,
        "evaluated_at": at.isoformat(),
        "fresh_until": (observed + timedelta(seconds=max_age)).isoformat() if valid else None,
    }


def score_change(row: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    early = row.get("early") or {}
    old = (previous or {}).get("features") or {}
    current_score, previous_score = number(early.get("score")), number(old.get("score"))
    current_time, previous_time = (
        moment(row.get("observed_at")),
        moment((previous or {}).get("observed_at")),
    )
    status = "pending"
    if current_time and previous_time and previous_time < current_time:
        if early.get("version") and old.get("version"):
            if early["version"] != old["version"]:
                status = "model_changed"
            elif current_score is not None and previous_score is not None:
                status = "ready"
    return {
        "status": status,
        "previous_at": (previous or {}).get("observed_at"),
        "previous_score": previous_score,
        "previous_state": old.get("state"),
        "score_delta": round(current_score - previous_score, 2) if status == "ready" else None,
        "elapsed_seconds": int((current_time - previous_time).total_seconds())
        if status == "ready"
        else None,
    }


def token_insights(
    row: dict[str, Any],
    basics: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    current = at or datetime.now(UTC)
    early = row.get("early") or {}
    model = row.get("memecoin_assessment") or {}
    sentiment = model.get("sentiment") or {}
    coverage = model.get("coverage") or {}
    risk = model.get("risk") or {}
    ratification = row.get("ratification") or row.get("ratify") or {}
    standards = {item["key"]: item for item in ratification.get("standards", [])}
    checks = []
    for key, label in CHECK_LABELS.items():
        item = standards.get(key) or {}
        value = item.get("met")
        state = (
            "skipped"
            if item.get("applies") is False
            else "met"
            if value is True
            else "flagged"
            if value is False
            else "pending"
        )
        checks.append(
            {
                "key": key,
                "label": label,
                "status": state,
                "detail": item.get("detail", ""),
                "basis": "Largest token account sample"
                if key == "holders"
                else "Saved RATi standard",
            }
        )
    counts = {
        state: sum(item["status"] == state for item in checks)
        for state in ("met", "flagged", "pending", "skipped")
    }
    return {
        "version": VERSION,
        "freshness": freshness(row.get("observed_at"), at=current),
        "momentum": {
            "model_version": early.get("version"),
            "score": number(early.get("score")),
            "state": early.get("state"),
            "reasons": [str(value) for value in early.get("reasons", [])[:8]],
            "change": score_change(row, previous),
        },
        "checks": {
            "status": "review"
            if counts["flagged"]
            else "incomplete"
            if counts["pending"]
            else "checks_met"
            if counts["met"]
            else "pending",
            **counts,
            "items": checks,
            "freshness": freshness(ratification.get("as_of"), at=current),
        },
        "controls_freshness": freshness((basics or {}).get("checked_at"), at=current, max_age=300),
        "activity": {
            "state": sentiment.get("state", "unknown"),
            "basis": sentiment.get("basis"),
            "window_start": sentiment.get("window_start"),
            "window_end": sentiment.get("window_end"),
            "observed_swaps": sentiment.get("observed_swaps"),
            "net_buyers": sentiment.get("net_buyers"),
            "net_sellers": sentiment.get("net_sellers"),
            "balanced_wallets": sentiment.get("balanced_wallets"),
            "freshness": freshness(coverage.get("chain_checked_at"), at=current),
            "collection_partial": coverage.get("collection_partial", True),
            "coverage_gaps": coverage.get("recorded_coverage_gaps"),
        },
        "open_checks": [str(value) for value in risk.get("missing_checks", [])[:12]],
        "risk_evidence": {
            "state": risk.get("state", "unknown"),
            "factors": risk.get("factors", [])[:20],
            "window_start": risk.get("window_start"),
            "window_end": risk.get("window_end"),
        },
    }


def percent(part: int | float, total: int | float) -> float | None:
    return round(100 * part / total, 2) if total > 0 else None


def wallet_insights(data: dict[str, Any], *, at: datetime | None = None) -> dict[str, Any]:
    current = at or datetime.now(UTC)
    holdings = data.get("holdings") or []
    assets = [item for item in holdings if item.get("mint") not in {SOL, USDC}]
    priced = [
        item
        for item in holdings
        if (value := number(item.get("value_usd"))) is not None and value >= 0
    ]
    total = math.fsum(number(item["value_usd"]) for item in priced)
    ordered = sorted(priced, key=lambda item: number(item["value_usd"]), reverse=True)
    known_costs = sum(number(item.get("cost")) is not None for item in assets)
    history = (
        "complete"
        if data.get("has_more") is False
        else ("partial" if data.get("has_more") is True else "unknown")
    )
    pnl = []
    for item in data.get("pnl") or []:
        known, unknown = int(item.get("closed_trades") or 0), int(item.get("unknown_sales") or 0)
        pnl.append(
            {
                "unit": item["unit"],
                "realized": number(item.get("realized")),
                "unrealized": number(item.get("unrealized")),
                "sales_with_known_cost": known,
                "sales_with_unknown_cost": unknown,
                "sale_cost_coverage_pct": percent(known, known + unknown),
                "priced_positions": item.get("priced_positions"),
            }
        )
    trades = (data.get("trades") or [])[:200]
    sales_by_unit = []
    for unit in ("SOL", "USDC"):
        sales = [item for item in trades if item.get("side") == "Sell" and item.get("unit") == unit]
        gains = [
            gain
            for item in sales
            if item.get("cost_known") is True and (gain := number(item.get("realized"))) is not None
        ]
        wins, losses = sum(value > 0 for value in gains), sum(value < 0 for value in gains)
        sales_by_unit.append(
            {
                "unit": unit,
                "sales": len(sales),
                "known_cost_sales": len(gains),
                "unknown_cost_sales": len(sales) - len(gains),
                "wins": wins,
                "losses": losses,
                "flat": len(gains) - wins - losses,
                "win_rate_pct": percent(wins, len(gains)),
                "realized": math.fsum(gains) if gains else None,
            }
        )
    times = [stamp for item in trades if (stamp := moment(item.get("at")))]
    return {
        "version": VERSION,
        "freshness": freshness(data.get("updated_at"), at=current),
        "coverage": {
            "history": history,
            "oldest_at": data.get("oldest_at"),
            "newest_at": data.get("newest_at"),
            "transactions": data.get("transactions"),
            "excluded_transactions": data.get("excluded_transactions"),
            "asset_positions": len(assets),
            "known_cost_positions": known_costs,
            "unknown_cost_positions": len(assets) - known_costs,
            "position_cost_coverage_pct": percent(known_costs, len(assets)),
            "scope": "Saved transactions and current token positions",
        },
        "exposure": {
            "basis": "Priced token holdings",
            "holdings": len(holdings),
            "priced_holdings": len(priced),
            "unpriced_holdings": len(holdings) - len(priced),
            "pricing_coverage_pct": percent(len(priced), len(holdings)),
            "priced_value_usd": total if priced else None,
            "native_sol_balance": number(data.get("balance_sol")),
            "top_five_share_pct": percent(
                math.fsum(number(item["value_usd"]) for item in ordered[:5]), total
            ),
            "top_positions": [
                {
                    "mint": item["mint"],
                    "name": item.get("name"),
                    "value_usd": number(item["value_usd"]),
                    "share_of_priced_value_pct": percent(number(item["value_usd"]), total),
                    "cost_known": number(item.get("cost")) is not None,
                }
                for item in ordered[:5]
            ],
        },
        "pnl_by_quote": pnl,
        "fees_sol": number(data.get("fees_sol")),
        "recent_sales": {
            "scope": "Most recent 200 recorded trades",
            "sample_trades": len(trades),
            "first_at": min(times).isoformat() if times else None,
            "last_at": max(times).isoformat() if times else None,
            "by_quote": sales_by_unit,
        },
    }
