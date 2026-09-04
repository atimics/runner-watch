from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

EASTERN = ZoneInfo("America/New_York")
FIBONACCI_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)


@dataclass(frozen=True, slots=True)
class StructureFeatures:
    opening_range_position: float = 0.5
    opening_range_breakout_pct: float = 0.0
    support_distance_pct: float = 0.0
    support_strength: float = 0.0
    resistance_distance_pct: float = 0.0
    resistance_strength: float = 0.0
    fib_retracement_pct: float = 0.0
    fib_level_distance_pct: float = 0.0
    structure_available: bool = False
    fibonacci_available: bool = False


@dataclass(frozen=True, slots=True)
class StructureAnalysis:
    features: StructureFeatures = field(default_factory=StructureFeatures)
    levels: tuple[dict[str, Any], ...] = ()
    fibonacci: dict[str, Any] | None = None
    summary: dict[str, Any] = field(default_factory=dict)


def _column(frame: pd.DataFrame, name: str) -> pd.Series:
    wanted = name.lower().replace(" ", "")
    for column in frame.columns:
        if str(column).lower().replace(" ", "") == wanted:
            return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(index=frame.index, dtype="float64")


def clean_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:


    if frame is None or frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    index = pd.to_datetime(frame.index, errors="coerce", utc=True)
    clean = pd.DataFrame(index=index)
    for name in ("open", "high", "low", "close", "volume"):
        clean[name] = _column(frame, name).to_numpy()
    clean = clean.loc[~clean.index.isna()]
    clean = clean.loc[clean["close"].notna() & (clean["close"] > 0)].sort_index()
    clean["open"] = clean["open"].fillna(clean["close"])
    clean["high"] = clean["high"].fillna(clean[["open", "close"]].max(axis=1))
    clean["low"] = clean["low"].fillna(clean[["open", "close"]].min(axis=1))
    clean["high"] = clean[["open", "high", "low", "close"]].max(axis=1)
    clean["low"] = clean[["open", "high", "low", "close"]].min(axis=1)
    clean["volume"] = clean["volume"].fillna(0).clip(lower=0)
    clean.index = clean.index.tz_convert(EASTERN)
    return clean


def _median_true_range(frame: pd.DataFrame) -> float:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    values = true_range.replace([math.inf, -math.inf], pd.NA).dropna().tail(60)
    return float(values.median()) if not values.empty else 0.0


def _session_context(frame: pd.DataFrame) -> dict[str, Any]:
    latest = frame.index[-1]
    current = frame.loc[frame.index.date == latest.date()]
    regular = current.loc[
        [time(9, 30) <= stamp.time().replace(tzinfo=None) < time(16) for stamp in current.index]
    ]
    opening = regular.loc[
        [time(9, 30) <= stamp.time().replace(tzinfo=None) < time(10) for stamp in regular.index]
    ]
    prior = frame.loc[frame.index.date < latest.date()]
    prior_date = prior.index[-1].date() if not prior.empty else None
    prior_session = prior.loc[prior.index.date == prior_date] if prior_date else prior
    prior_regular = prior_session.loc[
        [
            time(9, 30) <= stamp.time().replace(tzinfo=None) < time(16)
            for stamp in prior_session.index
        ]
    ]
    if not prior_regular.empty:
        prior_session = prior_regular
    vwap_frame = regular if not regular.empty else current
    typical = (vwap_frame["high"] + vwap_frame["low"] + vwap_frame["close"]) / 3
    weights = vwap_frame["volume"]
    vwap = (
        float((typical * weights).sum() / weights.sum())
        if float(weights.sum()) > 0
        else float(vwap_frame["close"].iloc[-1])
    )
    return {
        "current": current,
        "regular": regular,
        "opening": opening,
        "prior": prior_session,
        "vwap": vwap,
    }


def _pivots(frame: pd.DataFrame, window: int = 2) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    if len(frame) < window * 2 + 1:
        return values
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    for index in range(window, len(frame) - window):
        high_slice = highs[index - window : index + window + 1]
        low_slice = lows[index - window : index + window + 1]
        if highs[index] >= max(high_slice) and highs[index] > min(high_slice):
            values.append(
                {
                    "kind": "swing high",
                    "side": "high",
                    "price": float(highs[index]),
                    "time": frame.index[index],
                    "weight": 1.0,
                }
            )
        if lows[index] <= min(low_slice) and lows[index] < max(low_slice):
            values.append(
                {
                    "kind": "swing low",
                    "side": "low",
                    "price": float(lows[index]),
                    "time": frame.index[index],
                    "weight": 1.0,
                }
            )
    return sorted(values, key=lambda item: item["time"])


def _cluster_levels(
    candidates: list[dict[str, Any]], radius: float, current_price: float
) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: float(item["price"])):
        price = float(candidate["price"])
        if not math.isfinite(price) or price <= 0:
            continue
        target = next(
            (
                cluster
                for cluster in clusters
                if abs(price - float(cluster["center"])) <= radius * 2
            ),
            None,
        )
        if target is None:
            target = {
                "weighted_price": 0.0,
                "weight": 0.0,
                "touches": 0,
                "kinds": set(),
                "center": price,
            }
            clusters.append(target)
        weight = float(candidate.get("weight") or 1.0)
        target["weighted_price"] += price * weight
        target["weight"] += weight
        target["center"] = target["weighted_price"] / target["weight"]
        target["kinds"].add(str(candidate.get("kind") or "price"))
        if candidate.get("touch", True):
            target["touches"] += 1

    output: list[dict[str, Any]] = []
    for cluster in clusters:
        center = float(cluster["center"])
        strength = min(1.0, float(cluster["weight"]) / 6.0)
        if center < current_price - radius:
            side = "support"
        elif center > current_price + radius:
            side = "resistance"
        else:
            side = "current"
        output.append(
            {
                "type": "gravity",
                "side": side,
                "price": round(center, 6),
                "low": round(max(0.0, center - radius), 6),
                "high": round(center + radius, 6),
                "touches": int(cluster["touches"]),
                "strength": round(strength, 3),
                "kinds": sorted(cluster["kinds"]),
            }
        )
    return output


def _compressed_pivots(pivots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compressed: list[dict[str, Any]] = []
    for pivot in pivots:
        if not compressed or compressed[-1]["side"] != pivot["side"]:
            compressed.append(pivot)
            continue
        previous = compressed[-1]
        more_extreme = (
            pivot["price"] >= previous["price"]
            if pivot["side"] == "high"
            else pivot["price"] <= previous["price"]
        )
        if more_extreme:
            compressed[-1] = pivot
    return compressed


def _fibonacci_map(
    pivots: list[dict[str, Any]], current_price: float, median_range: float
) -> dict[str, Any] | None:
    compressed = _compressed_pivots(pivots)
    threshold = max(current_price * 0.03, median_range * 3)
    anchor: tuple[dict[str, Any], dict[str, Any]] | None = None
    for start, end in zip(compressed, compressed[1:], strict=False):
        if start["side"] == end["side"]:
            continue
        if abs(float(end["price"]) - float(start["price"])) < threshold:
            continue
        anchor = (start, end)
    if anchor is None:
        return None
    start, end = anchor
    start_price = float(start["price"])
    end_price = float(end["price"])
    span = abs(end_price - start_price)
    if span <= 0:
        return None
    direction = "up" if end_price > start_price else "down"
    levels: list[dict[str, Any]] = []
    for ratio in FIBONACCI_RATIOS:
        price = end_price - span * ratio if direction == "up" else end_price + span * ratio
        levels.append(
            {
                "type": "fibonacci",
                "ratio": ratio,
                "label": f"{ratio * 100:.1f}%",
                "price": round(price, 6),
            }
        )
    retracement = (
        (end_price - current_price) / span * 100
        if direction == "up"
        else (current_price - end_price) / span * 100
    )
    nearest = min(levels, key=lambda item: abs(float(item["price"]) - current_price))
    return {
        "direction": direction,
        "start": {
            "time": start["time"].isoformat(),
            "price": round(start_price, 6),
        },
        "end": {
            "time": end["time"].isoformat(),
            "price": round(end_price, 6),
        },
        "levels": levels,
        "retracement_pct": round(retracement, 3),
        "nearest_ratio": nearest["ratio"],
        "nearest_label": nearest["label"],
        "nearest_distance_pct": round(
            abs(float(nearest["price"]) - current_price) / current_price * 100, 4
        ),
        "note": "Crowd reference only; it does not change the hand-written score.",
    }


def analyze_market_structure(frame: pd.DataFrame) -> StructureAnalysis:


    clean = clean_ohlcv(frame)
    if len(clean) < 5:
        return StructureAnalysis()
    clean = clean.tail(1400)
    current_price = float(clean["close"].iloc[-1])
    median_range = _median_true_range(clean)
    radius = max(current_price * 0.0025, median_range * 0.35, 0.0001)
    context = _session_context(clean)
    pivots = _pivots(clean)
    candidates = list(pivots)

    def add_level(kind: str, value: float | None, weight: float) -> None:
        if value is not None and math.isfinite(value) and value > 0:
            candidates.append(
                {
                    "kind": kind,
                    "price": float(value),
                    "weight": weight,
                    "touch": False,
                }
            )

    prior = context["prior"]
    opening = context["opening"]
    current = context["current"]
    add_level("previous close", float(prior["close"].iloc[-1]) if not prior.empty else None, 2)
    add_level("previous high", float(prior["high"].max()) if not prior.empty else None, 1.5)
    add_level("previous low", float(prior["low"].min()) if not prior.empty else None, 1.5)
    add_level("session VWAP", float(context["vwap"]), 2)
    add_level(
        "opening range high",
        float(opening["high"].max()) if not opening.empty else None,
        1.5,
    )
    add_level("opening range low", float(opening["low"].min()) if not opening.empty else None, 1.5)
    add_level("day high", float(current["high"].max()), 1)
    add_level("day low", float(current["low"].min()), 1)

    recent = clean.tail(240)
    if not recent.empty and float(recent["volume"].max()) > 0:
        cutoff = float(recent["volume"].quantile(0.85))
        for stamp, row in recent.loc[recent["volume"] >= cutoff].iterrows():
            candidates.append(
                {
                    "kind": "heavy volume",
                    "price": float((row["high"] + row["low"] + row["close"]) / 3),
                    "weight": 0.6,
                    "touch": False,
                    "time": stamp,
                }
            )

    levels = _cluster_levels(candidates, radius, current_price)
    nearby = sorted(
        levels,
        key=lambda item: (
            abs(float(item["price"]) - current_price) / current_price,
            -float(item["strength"]),
        ),
    )[:8]
    nearby = sorted(nearby, key=lambda item: float(item["price"]), reverse=True)
    supports = [item for item in levels if float(item["low"]) <= current_price]
    resistances = [item for item in levels if float(item["high"]) >= current_price]
    support = max(supports, key=lambda item: float(item["price"]), default=None)
    resistance = min(resistances, key=lambda item: float(item["price"]), default=None)

    opening_position = 0.5
    opening_breakout = 0.0
    if not opening.empty:
        opening_high = float(opening["high"].max())
        opening_low = float(opening["low"].min())
        if opening_high > opening_low:
            opening_position = (current_price - opening_low) / (opening_high - opening_low)
            opening_breakout = (current_price / opening_high - 1) * 100

    fib = _fibonacci_map(pivots, current_price, median_range)
    features = StructureFeatures(
        opening_range_position=max(-2.0, min(3.0, opening_position)),
        opening_range_breakout_pct=max(-100.0, min(100.0, opening_breakout)),
        support_distance_pct=(
            max(0.0, (current_price - float(support["high"])) / current_price * 100)
            if support
            else 0.0
        ),
        support_strength=float(support["strength"]) if support else 0.0,
        resistance_distance_pct=(
            max(0.0, (float(resistance["low"]) - current_price) / current_price * 100)
            if resistance
            else 0.0
        ),
        resistance_strength=float(resistance["strength"]) if resistance else 0.0,
        fib_retracement_pct=(
            max(-100.0, min(200.0, float(fib["retracement_pct"]))) if fib else 0.0
        ),
        fib_level_distance_pct=float(fib["nearest_distance_pct"]) if fib else 0.0,
        structure_available=bool(levels),
        fibonacci_available=fib is not None,
    )
    summary = {
        "price": round(current_price, 6),
        "vwap": round(float(context["vwap"]), 6),
        "previous_close": (
            round(float(prior["close"].iloc[-1]), 6) if not prior.empty else None
        ),
        "opening_range": (
            {
                "low": round(float(opening["low"].min()), 6),
                "high": round(float(opening["high"].max()), 6),
            }
            if not opening.empty
            else None
        ),
        "support": support,
        "resistance": resistance,
        "zone_width": round(radius * 2, 6),
    }
    return StructureAnalysis(
        features=features,
        levels=tuple(nearby),
        fibonacci=fib,
        summary=summary,
    )
