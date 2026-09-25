"""Golf outcome quotes with explicit settlement terms and saved source receipts."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from runner_watch.ingestion import SourceFetch
from runner_web.db import connection
from runner_web.golf_cup import EVENT_ID
from runner_web.ingestion import record_source_fetch
from runner_web.sports_markets import KALSHI_ROOT, POLY_ROOT, _array, _get_json, _number, _time

CUP_KALSHI = "KXPRESCUP-26"
CUP_POLY = "presidents-cup-2026"


def _quote(
    source: str,
    contract: str,
    outcome: str,
    market: dict[str, Any],
    at: datetime,
    *,
    probability: float,
    bid: float | None,
    ask: float | None,
    rules: str,
) -> dict[str, Any]:
    spread = ask - bid if bid is not None and ask is not None else None
    quality = "quoted" if spread is not None and 0 <= spread <= 0.20 else "wide spread"
    return {
        "event_id": EVENT_ID,
        "contract_key": contract,
        "outcome_key": outcome,
        "source": source,
        "observed_at": at.isoformat(),
        "probability": probability,
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "quality": quality,
        "source_market_id": str(market.get("ticker") or market.get("id") or ""),
        "source_updated_at": market.get("updated_time") or market.get("updatedAt"),
        "rules": rules,
        "url": (
            f"https://kalshi.com/markets/kxprescup/presidents-cup/{CUP_KALSHI.lower()}"
            if source == "kalshi"
            else f"https://polymarket.com/event/{CUP_POLY}"
        ),
        "basis": "Yes bid/ask midpoint" if source == "kalshi" else "Listed outcome price",
        "liquidity": _number(market.get("liquidityNum")),
        "volume": _number(market.get("volume_fp") or market.get("volumeNum")),
        "volume_24h": _number(
            market.get("volume_24h_fp") if source == "kalshi" else market.get("volume24hr")
        ),
        "volume_unit": "contracts" if source == "kalshi" else "USD",
        "volume_scope": "outcome market" if source == "kalshi" else "whole Cup market",
    }


def normalize_cup_kalshi(payload: dict[str, Any], at: datetime) -> list[dict[str, Any]]:
    result = []
    for event in payload.get("events") or []:
        if event.get("event_ticker") != CUP_KALSHI or not event.get("mutually_exclusive"):
            continue
        for market in event.get("markets") or []:
            key = {"USA": "usa", "WORLD": "international", "TIE": "tie"}.get(
                str(market.get("ticker") or "").removeprefix(CUP_KALSHI + "-")
            )
            rules = (
                str(market.get("rules_primary") or "")
                + " "
                + str(market.get("rules_secondary") or "")
            )
            if (
                key is None
                or market.get("status") != "active"
                or market.get("market_type") != "binary"
                or "2026 Presidents Cup" not in rules
                or "tie" not in rules.lower()
            ):
                continue
            bid, ask = (
                _number(market.get("yes_bid_dollars")),
                _number(market.get("yes_ask_dollars")),
            )
            if bid is None or ask is None or not 0 <= bid <= ask <= 1:
                continue
            updated = _time(market.get("updated_time"))
            if updated is None or updated > at:
                continue
            result.append(
                _quote(
                    "kalshi",
                    "winner",
                    key,
                    market,
                    at,
                    probability=(bid + ask) / 2,
                    bid=bid,
                    ask=ask,
                    rules=rules,
                )
            )
    return result


def normalize_cup_polymarket(payload: list[dict[str, Any]], at: datetime) -> list[dict[str, Any]]:
    result = []
    for event in payload:
        if event.get("slug") != CUP_POLY or event.get("closed") or not event.get("active"):
            continue
        for market in event.get("markets") or []:
            rules = str(market.get("description") or "")
            outcomes, prices = _array(market.get("outcomes")), _array(market.get("outcomePrices"))
            if (
                market.get("closed")
                or not market.get("active")
                or set(outcomes) != {"USA", "International"}
                or len(prices) != 2
                or "15-15 tie" not in rules
                or "resolve 50-50" not in rules
                or "2026 Presidents Cup" not in rules
            ):
                continue
            updated = _time(market.get("updatedAt"))
            if updated is None or updated > at:
                continue
            bid, ask = _number(market.get("bestBid")), _number(market.get("bestAsk"))
            if bid is not None and ask is not None and not 0 <= bid <= ask <= 1:
                bid, ask = None, None
            for index, label in enumerate(outcomes):
                price = _number(prices[index])
                if price is None or not 0 <= price <= 1:
                    continue
                side_bid = bid if index == 0 else 1 - ask if ask is not None else None
                side_ask = ask if index == 0 else 1 - bid if bid is not None else None
                result.append(
                    _quote(
                        "polymarket",
                        "winner-half-tie",
                        label.lower(),
                        market,
                        at,
                        probability=price,
                        bid=side_bid,
                        ask=side_ask,
                        rules=rules,
                    )
                )
    return result


def store_quotes(quotes: list[dict[str, Any]]) -> int:
    saved = 0
    with connection() as database:
        for row in quotes:
            saved += max(
                0,
                database.execute(
                    """INSERT INTO prediction_contract_quotes
                (event_id,contract_key,outcome_key,source,observed_at,payload_json)
                VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (
                        *[
                            row[key]
                            for key in (
                                "event_id",
                                "contract_key",
                                "outcome_key",
                                "source",
                                "observed_at",
                            )
                        ],
                        json.dumps(row, sort_keys=True, allow_nan=False),
                    ),
                ).rowcount,
            )
    return saved


def quote_history(event_id: str, *, latest: bool = False) -> list[dict[str, Any]]:
    with connection() as database:
        rows = database.execute(
            "SELECT payload_json FROM (SELECT *,ROW_NUMBER() OVER ("
            "PARTITION BY contract_key,outcome_key,source ORDER BY observed_at DESC) AS n "
            "FROM prediction_contract_quotes WHERE event_id=?) ranked "
            + ("WHERE n=1 " if latest else "")
            + "ORDER BY observed_at DESC LIMIT 6000",
            (event_id,),
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in reversed(rows)]


def refresh_cup_markets(at: datetime | None = None) -> dict[str, Any]:
    if os.getenv("SPORTS_PREDICTION_MARKETS_ENABLED", "false").lower() in {"0", "false", "off"}:
        return {"enabled": False}
    current = at or datetime.now(UTC)
    result: dict[str, Any] = {"enabled": True, "saved": {}, "errors": {}}
    for source, url, normalize in [
        (
            "kalshi",
            f"{KALSHI_ROOT}/events?series_ticker=KXPRESCUP&with_nested_markets=true&limit=20",
            normalize_cup_kalshi,
        ),
        ("polymarket", f"{POLY_ROOT}/events?slug={CUP_POLY}", normalize_cup_polymarket),
    ]:
        try:
            payload = _get_json(url)
            result["saved"][source] = store_quotes(normalize(payload, current))
            record_source_fetch(
                SourceFetch.success(
                    source=source,
                    feed="golf_outcome_prices",
                    locator=url,
                    started_at=current,
                    payload=payload,
                )
            )
        except Exception as exc:
            result["errors"][source] = str(exc)[:160]
            record_source_fetch(
                SourceFetch.failure(
                    source=source,
                    feed="golf_outcome_prices",
                    locator=url,
                    started_at=current,
                    error=exc,
                )
            )
    return result
