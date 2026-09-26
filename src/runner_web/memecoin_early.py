"""Which coins are setting up: activity speeding up before the price has run.

The attention score reads the last 24 hours, so it ranks a coin highest after
its move, up or down. This reading compares the latest hour with the coin's
own pace over the previous six, and only counts it while the price has not
yet run and nothing on the chain says the launch is being drained.

States use the stock tags: setup (speeding up, price not yet moved),
running, extended, avoid (drained or staged) and quiet (no tag).

The weights are a starting heuristic (`memecoin-early-v1`). Each quote saves
its features so they can be tested against what the coin did next.
"""

from __future__ import annotations

import math
from typing import Any

VERSION = "memecoin-early-v1"
# Below this the hour's trading is too small to read a pace from.
MIN_HOUR_VOLUME_USD = 1_000.0
MIN_HOUR_BUYERS = 10
# The last five minutes against the hour: how a coin under an hour old shows pace.
MIN_M5_VOLUME_USD = 500.0
MIN_M5_BUYERS = 5
# A price already this far up has run; the reading is for before that.
RUN_H1_PCT = 50.0
RUN_H6_PCT = 150.0
EXTENDED_H6_PCT = 300.0
EXTENDED_24H_PCT = 500.0
DUMP_H1_PCT = -40.0
MIN_LIQUIDITY_USD = 5_000.0
# Chain findings that mean the launch is being drained or its buying is staged.
BLOCKING_KINDS = {
    "creator_sell": "Creator is selling",
    "liquidity_withdrawal": "Liquidity was withdrawn",
    "synchronized_buys": "Buying looks staged",
    "common_funder": "Buyers share one funder",
}
FEATURE_KEYS = (
    "liquidity_usd",
    "change_m5",
    "change_h1",
    "change_h6",
    "change_24h",
    "volume_m5",
    "volume_h1",
    "volume_h6",
    "buyers_m5",
    "buyers_h1",
    "buyers_h6",
    "sellers_h1",
    "sellers_h6",
    "organic_buyers",
)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pace(recent: float | None, earlier: float | None, periods: float) -> float | None:
    """How many times faster the latest period ran than the earlier periods' average."""

    if recent is None or earlier is None or recent <= 0:
        return None
    before = max(earlier - recent, 0.0) / (periods - 1)
    return recent / before if before > 0 else None


def _points(pace: float | None, cap: float) -> float:
    # Twice the usual pace earns a third of the cap; four times, two thirds.
    return min(cap, cap / 3 * math.log2(pace)) if pace and pace > 1 else 0.0


def _faster(hourly: float | None, recent: float | None) -> tuple[float | None, str]:
    """The stronger of the hour-vs-6h and 5m-vs-hour paces, and its window."""

    if recent is not None and (hourly is None or recent > hourly):
        return recent, "1h"
    return hourly, "6h"


def early_signal(row: dict[str, Any]) -> dict[str, Any]:
    """Score one quoted coin for activity picking up before a run."""

    features = {key: _finite(row.get(key)) for key in FEATURE_KEYS}
    counts = row.get("chain_sentiment_counts") or {}
    features["organic_buyers"] = _finite(counts.get("bullish"))
    on_curve = row.get("venue") == "bonding_curve"
    features["on_curve"] = on_curve
    factors = ((row.get("memecoin_assessment") or {}).get("risk") or {}).get("factors") or []
    blocked = list(
        dict.fromkeys(
            BLOCKING_KINDS[factor["kind"]]
            for factor in factors
            if isinstance(factor, dict) and factor.get("kind") in BLOCKING_KINDS
        )
    )
    liquidity = features["liquidity_usd"]
    # A bonding curve always quotes; there is no pool to drain until it graduates.
    if not on_curve and liquidity is not None and liquidity < MIN_LIQUIDITY_USD:
        blocked.append("Pool is too thin")
    change_24h, change_h1 = features["change_24h"], features["change_h1"]
    if change_24h is not None and change_24h <= -90:
        blocked.append("Already collapsed")
    elif change_h1 is not None and change_h1 <= DUMP_H1_PCT:
        blocked.append("Dumping this hour")
    result: dict[str, Any] = {"version": VERSION, "features": features, "score": None}
    if blocked:
        return {**result, "state": "avoid", "reasons": blocked}
    volume_h1, buyers_h1 = features["volume_h1"], features["buyers_h1"]
    if (
        volume_h1 is None
        or volume_h1 < MIN_HOUR_VOLUME_USD
        or buyers_h1 is None
        or buyers_h1 < MIN_HOUR_BUYERS
    ):
        return {**result, "state": "quiet", "reasons": []}
    volume_pace, volume_window = _faster(
        _pace(volume_h1, features["volume_h6"], 6),
        _pace(features["volume_m5"], volume_h1, 12)
        if (features["volume_m5"] or 0) >= MIN_M5_VOLUME_USD
        else None,
    )
    buyer_pace, buyer_window = _faster(
        _pace(buyers_h1, features["buyers_h6"], 6),
        _pace(features["buyers_m5"], buyers_h1, 12)
        if (features["buyers_m5"] or 0) >= MIN_M5_BUYERS
        else None,
    )
    sellers_h1 = features["sellers_h1"] or 0.0
    buyer_share = buyers_h1 / (buyers_h1 + sellers_h1)
    organic = features["organic_buyers"] or 0.0
    parts = {
        "volume_pace": _points(volume_pace, 35),
        "buyer_pace": _points(buyer_pace, 35),
        "buyer_share": min(20.0, max(0.0, (buyer_share - 0.5) * 80)),
        "organic_buyers": min(10.0, 2.5 * math.log2(1 + organic)),
    }
    score = round(sum(parts.values()), 1)
    reasons = []
    if volume_pace and volume_pace >= 1.5:
        reasons.append(f"Volume {volume_pace:.1f}× its {volume_window} pace")
    if buyer_pace and buyer_pace >= 1.5:
        reasons.append(f"Buyers {buyer_pace:.1f}× their {buyer_window} pace")
    if buyer_share >= 0.55:
        reasons.append(f"{buyer_share:.0%} of traders buying")
    if organic >= 3:
        reasons.append(f"{organic:.0f} unlinked wallets buying")
    change_h6 = features["change_h6"]
    extended = (change_h6 is not None and change_h6 >= EXTENDED_H6_PCT) or (
        change_24h is not None and change_24h >= EXTENDED_24H_PCT
    )
    running = (change_h1 is not None and change_h1 >= RUN_H1_PCT) or (
        change_h6 is not None and change_h6 >= RUN_H6_PCT
    )
    state = (
        "extended"
        if extended
        else "running"
        if running
        else "setup"
        if score >= 30 and reasons
        else "quiet"
    )
    return {**result, "score": score, "parts": parts, "state": state, "reasons": reasons}
