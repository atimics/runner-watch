"""Versioned attention, sampled swap balance and evidence-backed token risk."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from runner_web.attention import finite_number

VERSION = "memecoin-sar-v1"
FRESH_SECONDS = 900
RISK_SECONDS = 86400
MISSING_CHECKS = [
    "Token control powers",
    "Holder concentration",
    "Sale proceeds at a stated position size",
    "External sentiment and paid promotion",
]
# These are declared attention policy points, to be tested on later cohorts.
EVENT_POINTS = {
    "creator_buy": 5.0,
    "creator_sol_transfer": 5.0,
    "common_funder": 10.0,
    "synchronized_buys": 10.0,
    "repeated_round_trips": 10.0,
    "creator_sell": 15.0,
    "liquidity_withdrawal": 25.0,
}
RISK_KINDS = {
    "creator_sell",
    "liquidity_withdrawal",
    "common_funder",
    "synchronized_buys",
    "repeated_round_trips",
}
FILTER_KINDS = {"common_funder", "synchronized_buys", "repeated_round_trips"}


def _time(value: Any) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(value))
        return value.astimezone(UTC) if value.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _recent(value: Any, at: datetime, seconds: int, *, future: int = 0) -> bool:
    stamp = _time(value)
    return stamp is not None and -future <= (at - stamp).total_seconds() <= seconds


def assess_memecoin(
    row: dict[str, Any],
    *,
    events: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    coverage: dict[str, Any],
    at: datetime,
) -> dict[str, Any]:
    """Assess saved receipts with explicit windows and partial collection coverage.

    Swap balance uses each wallet's net token change in the sampled swaps. Known
    launch-linked and pattern-linked wallets are counted in the exclusions. Market
    activity and material chain events contribute separate attention points.
    """
    token = row.get("token_address") if row.get("network") == "solana" else None
    chain_fresh = bool(token) and _recent(coverage.get("checked_at"), at, FRESH_SECONDS)
    price = finite_number(row.get("price"))
    quote_fresh = (
        price is not None
        and price > 0
        and not row.get("stale")
        and _recent(row.get("observed_at"), at, FRESH_SECONDS, future=60)
    )
    # A finding needs this token, a bounded event time and source receipts.
    saved_findings = {
        finding["id"]: finding
        for finding in findings
        if token
        and finding.get("token_address") == token
        and finding.get("id")
        and finding.get("kind") in EVENT_POINTS
        and _recent(finding.get("observed_at"), at, RISK_SECONDS)
        and isinstance(finding.get("evidence"), list)
        and any(proof.get("signature") for proof in finding["evidence"] if isinstance(proof, dict))
    }
    selected = sorted(saved_findings.values(), key=lambda item: item["id"])
    excluded: dict[str, set[str]] = defaultdict(set)
    for event in events:
        if (
            token
            and event.get("token_address") == token
            and event.get("kind") in {"token_launch", "pool_created"}
            and _time(event.get("observed_at")) is not None
            and _time(event["observed_at"]) <= at
        ):
            for wallet in (event.get("wallet"), event.get("declared_creator")):
                if wallet and wallet != "1" * 32:
                    excluded[wallet].add("launch_link")
    for finding in selected:
        if finding["kind"] in {"creator_buy", "creator_sell", "creator_sol_transfer"}:
            if finding.get("wallet"):
                excluded[finding["wallet"]].add("launch_link")
        if finding["kind"] in FILTER_KINDS:
            wallets = list(finding.get("related_wallets") or [])
            if finding["kind"] == "repeated_round_trips" and finding.get("wallet"):
                wallets.append(finding["wallet"])
            for wallet in wallets:
                excluded[wallet].add(finding["kind"])

    swaps = {}
    for event in events:
        if (
            not chain_fresh
            or event.get("token_address") != token
            or event.get("kind") != "swap"
            or event.get("direction") not in {"buy", "sell"}
            or not event.get("wallet")
            or not event.get("signature")
            or not _recent(event.get("observed_at"), at, FRESH_SECONDS)
        ):
            continue
        try:
            amount = Decimal(str(event.get("net_token_amount")))
        except InvalidOperation:
            continue
        if not amount.is_finite() or amount <= 0:
            continue
        key = (event["signature"], event["wallet"], event["direction"])
        swaps.setdefault(key, (event, amount))
    balances: dict[str, Decimal] = defaultdict(Decimal)
    proofs = []
    observed_wallets = set()
    for event, amount in swaps.values():
        wallet = event["wallet"]
        observed_wallets.add(wallet)
        if wallet in excluded:
            continue
        balances[wallet] += amount if event["direction"] == "buy" else -amount
        proofs.append(
            {
                "event_id": event.get("event_id"),
                "signature": event["signature"],
                "receipt_url": "/api/memecoins/evidence/" + event["signature"],
            }
        )
    buying = sum(value > 0 for value in balances.values())
    selling = sum(value < 0 for value in balances.values())
    flat = sum(value == 0 for value in balances.values())
    filtered = sorted(observed_wallets & excluded.keys())
    counts = {"bullish": buying, "bearish": selling} if buying + selling else None
    tone = "positive" if buying > selling else "negative" if selling > buying else "mixed"
    sentiment_basis = (
        (
            f"15-minute swap sample: {buying} net buying wallets, {selling} net selling "
            f"wallets, {flat} balanced wallets; {len(filtered)} linked or pattern wallets "
            "excluded. Wallets are on-chain addresses. Collection covers part of the market."
        )
        if balances
        else "No swaps were sampled in the last 15 minutes."
    )
    change = finite_number(row.get("change_24h")) if quote_fresh else None
    volume = finite_number(row.get("volume_24h")) if quote_fresh else None
    move_points = min(35.0, abs(change)) if change is not None else None
    volume_points = (
        min(15.0, 5.0 * math.log10(1.0 + volume / 1000.0))
        if volume is not None and volume >= 0
        else None
    )
    market = (
        round((move_points or 0.0) + (volume_points or 0.0), 2)
        if move_points is not None or volume_points is not None
        else None
    )
    active_findings = [
        finding
        for finding in selected
        if chain_fresh and _recent(finding["observed_at"], at, FRESH_SECONDS)
    ]
    event_points = max((EVENT_POINTS[f["kind"]] for f in active_findings), default=0.0)
    wallet_points = min(25.0, 5.0 * math.log2(1.0 + buying + selling))
    chain = round(wallet_points + event_points, 2) if swaps or active_findings else None
    score = (
        round((market or 0.0) + (chain or 0.0), 2)
        if (market is not None or chain is not None)
        else None
    )
    factors = [finding for finding in selected if finding["kind"] in RISK_KINDS]
    risk_labels = list(dict.fromkeys(str(finding["title"]) for finding in factors))
    snapshot = {
        "version": VERSION,
        "as_of": at.isoformat(),
        "state": "partial",
        "attention": {
            "value": score,
            "price_move": move_points,
            "reported_volume": volume_points,
            "sampled_wallets": round(wallet_points, 2),
            "material_event": event_points,
            "basis": "Direction-neutral activity points; policy weights await outcome testing.",
        },
        "sentiment": {
            "state": "sampled" if counts else "unknown",
            "basis": sentiment_basis,
            "window_start": (at - timedelta(seconds=FRESH_SECONDS)).isoformat(),
            "window_end": at.isoformat(),
            "net_buyers": buying,
            "net_sellers": selling,
            "balanced_wallets": flat,
            "observed_swaps": len(swaps),
            "excluded_wallets": [
                {"wallet": wallet, "reasons": sorted(excluded[wallet])} for wallet in filtered
            ],
            "evidence": sorted(
                proofs, key=lambda proof: (proof["signature"], proof["event_id"] or "")
            ),
        },
        "quote_inputs": {
            key: row.get(key)
            for key in (
                "network",
                "token_address",
                "pool_address",
                "price",
                "change_24h",
                "volume_24h",
                "observed_at",
                "source_url",
            )
        },
        "risk": {
            "state": "detected" if factors else "unknown",
            "factors": factors,
            "window_start": (at - timedelta(seconds=RISK_SECONDS)).isoformat(),
            "window_end": at.isoformat(),
            "missing_checks": list(MISSING_CHECKS),
        },
        "coverage": {
            "state": "partial",
            "quote_at": row.get("observed_at"),
            "chain_checked_at": coverage.get("checked_at"),
            "chain_fresh": chain_fresh,
            "quote_fresh": quote_fresh,
            "collection_partial": coverage.get("partial", True),
            "recorded_coverage_gaps": coverage.get("recorded_coverage_gaps"),
            "program_backlog_seconds": {
                stream["stream"]: stream.get("backlog_seconds")
                for stream in coverage.get("streams") or []
                if str(stream.get("stream") or "").startswith("program:")
            },
            "note": "Sampled chain activity. Control, ownership and sale checks await data.",
        },
    }
    return {
        "memecoin_assessment": snapshot,
        "attention_score": score,
        "score_components": {"market": market, "chain_event": chain, "social_search": None},
        "score_as_of": at.isoformat(),
        "chain_sentiment": tone if counts else None,
        "chain_sentiment_counts": counts,
        "chain_sentiment_basis": sentiment_basis,
        "risks": risk_labels,
    }


def display_assessment(row: dict[str, Any], *, at: datetime) -> dict[str, Any]:
    """Keep the saved receipt and expire its current score and direction together."""
    model = row.get("memecoin_assessment")
    if not isinstance(model, dict) or model.get("version") != VERSION:
        return row
    row = {
        **row,
        "risks": list(
            dict.fromkeys(
                str(factor["title"])
                for factor in model["risk"]["factors"]
                if _recent(factor.get("observed_at"), at, RISK_SECONDS)
            )
        ),
    }
    if not row.get("stale") and _recent(model.get("as_of"), at, FRESH_SECONDS):
        return row
    return {
        **row,
        "memecoin_assessment": {**model, "state": "stale"},
        "attention_score": None,
        "score_components": {},
        "chain_sentiment": None,
        "chain_sentiment_counts": None,
        "chain_sentiment_basis": "Refresh the quote and chain evidence for a current reading.",
    }
