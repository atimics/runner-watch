"""Evidence-linked wallet relationships and trading patterns, without identity claims."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any


def analyze_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    creators = defaultdict(dict)
    links = []
    trades = defaultdict(list)
    findings = {}
    metrics = {}

    def finding(kind, title, rows, token=None, wallet=None, basis="observation", **extra):
        ids = sorted({row["event_id"] for row in rows})
        identity = hashlib.sha256((kind + "|".join(ids)).encode()).hexdigest()
        latest = max(rows, key=lambda row: row["observed_at"])
        findings[identity] = {
            "id": identity,
            "kind": kind,
            "title": title,
            "basis": basis,
            "token_address": token or "",
            "wallet": wallet or "",
            "observed_at": latest["observed_at"],
            "signature": latest["signature"],
            "source_url": latest["source_url"],
            "evidence": [
                {
                    "event_id": row["event_id"],
                    "signature": row["signature"],
                    "source_url": row["source_url"],
                    "receipt_url": "/api/memecoins/evidence/" + row["signature"],
                    "kind": row["kind"],
                }
                for row in rows
            ],
            **extra,
        }

    for event in events:
        if event["kind"] in ("pool_created", "token_launch"):
            token = event["token_address"]
            creators[token][event["wallet"]] = event
            if event.get("declared_creator") and event["declared_creator"] != "1" * 32:
                creators[token][event["declared_creator"]] = event
        elif event["kind"] == "sol_transfer":
            links.append(event)
        elif event["kind"] == "swap":
            trades[event["token_address"]].append(event)

    for token, rows in trades.items():
        # Net changes refer to the transaction, even if multiple swap instructions occur.
        distinct = {(row["signature"], row["wallet"], row["direction"]): row for row in rows}
        rows = sorted(distinct.values(), key=lambda row: row["observed_at"])
        metrics[token] = {
            "observed_swaps": len(rows),
            "observed_buyers": len({row["wallet"] for row in rows if row["direction"] == "buy"}),
            "observed_sellers": len({row["wallet"] for row in rows if row["direction"] == "sell"}),
        }
        for row in rows:
            relationship = creators[token].get(row["wallet"])
            if relationship and relationship["observed_at"] <= row["observed_at"]:
                finding(
                    "creator_" + row["direction"],
                    "Launch-linked wallet " + ("sold" if row["direction"] == "sell" else "bought"),
                    [relationship, row],
                    token,
                    row["wallet"],
                    net_token_amount=row["net_token_amount"],
                    relationship_source_url=relationship["source_url"],
                    role_label="Wallet named in launch or pool creation",
                )

        # A shared funder is an observed relationship, not a common-owner assertion.
        buyers = {row["wallet"]: row for row in reversed(rows) if row["direction"] == "buy"}
        funders = defaultdict(dict)
        for link in links:
            buy = buyers.get(link["wallet"])
            if (
                buy
                and link["observed_at"] <= buy["observed_at"]
                and link["source_wallet"] != link["wallet"]
            ):
                funders[link["source_wallet"]].setdefault(link["wallet"], (link, buy))
        for funder, wallets in funders.items():
            if 2 <= len(wallets) <= 25:
                proof = [row for pair in wallets.values() for row in pair]
                finding(
                    "common_funder",
                    "Buyers share a SOL funding source",
                    proof,
                    token,
                    funder,
                    basis="relationship",
                    related_wallets=sorted(wallets),
                    explanation="Shared services can also create this funding pattern.",
                )

        buys = [row for row in rows if row["direction"] == "buy"]
        for index, first in enumerate(buys):
            start = datetime.fromisoformat(first["observed_at"]).timestamp()
            amount = Decimal(first["net_token_amount"])
            group = {}
            for row in buys[index : index + 100]:
                if datetime.fromisoformat(row["observed_at"]).timestamp() - start > 10:
                    break
                if abs(Decimal(row["net_token_amount"]) - amount) <= amount * Decimal("0.01"):
                    group.setdefault(row["wallet"], row)
            if len(group) >= 3:
                finding(
                    "synchronized_buys",
                    "Similar-size buys arrived within ten seconds",
                    list(group.values()),
                    token,
                    basis="pattern",
                    related_wallets=sorted(group),
                    explanation="Timing and size similarity are a review signal.",
                )
                break
        by_wallet = defaultdict(list)
        for row in rows:
            by_wallet[row["wallet"]].append(row)
        for wallet, wallet_rows in by_wallet.items():
            for index in range(max(0, len(wallet_rows) - 3)):
                group = wallet_rows[index : index + 4]
                if len(group) < 4 or any(
                    a["direction"] == b["direction"] for a, b in zip(group, group[1:], strict=False)
                ):
                    continue
                duration = (
                    datetime.fromisoformat(group[-1]["observed_at"])
                    - datetime.fromisoformat(group[0]["observed_at"])
                ).total_seconds()
                bought = sum(
                    (
                        Decimal(row["net_token_amount"])
                        for row in group
                        if row["direction"] == "buy"
                    ),
                    Decimal(0),
                )
                sold = sum(
                    (
                        Decimal(row["net_token_amount"])
                        for row in group
                        if row["direction"] == "sell"
                    ),
                    Decimal(0),
                )
                if (
                    duration <= 600
                    and bought > 0
                    and abs(bought - sold) <= bought * Decimal("0.05")
                ):
                    finding(
                        "repeated_round_trips",
                        "Wallet made repeated buy-sell rounds",
                        group,
                        token,
                        wallet,
                        basis="pattern",
                        explanation="Arbitrage and market making can also produce round trips.",
                    )
                    break

    for event in events:
        if event["kind"] == "liquidity_withdrawal":
            finding(
                "liquidity_withdrawal",
                "Wallet withdrew pool liquidity",
                [event],
                event["token_address"],
                event["wallet"],
                net_token_amount=event["net_token_amount"],
            )
        if event["kind"] == "sol_transfer" and int(event["lamports"]) >= 1_000_000_000:
            for token, wallets in creators.items():
                relationship = wallets.get(event["source_wallet"])
                if relationship and relationship["observed_at"] <= event["observed_at"]:
                    finding(
                        "creator_sol_transfer",
                        "Launch-linked wallet moved SOL",
                        [relationship, event],
                        token,
                        event["source_wallet"],
                        recipient=event["wallet"],
                        lamports=event["lamports"],
                        explanation="The recipient is an on-chain address; its owner is unknown.",
                    )

    graph = [
        {
            "source": row["source_wallet"],
            "target": row["wallet"],
            "kind": "sol_transfer",
            "signature": row["signature"],
            "lamports": row["lamports"],
            "source_url": row["source_url"],
        }
        for row in links[:500]
    ]
    return {
        "findings": sorted(findings.values(), key=lambda row: row["observed_at"], reverse=True)[
            :250
        ],
        "wallet_links": graph,
        "token_metrics": metrics,
        "analyzed_events": len(events),
        "rules_version": 1,
        "analysis_window_start": min((row["observed_at"] for row in events), default=None),
        "analysis_window_end": max((row["observed_at"] for row in events), default=None),
    }
