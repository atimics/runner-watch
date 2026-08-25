from __future__ import annotations

import math
from dataclasses import dataclass


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True, slots=True)
class ScoreInput:
    change_pct: float
    momentum_5m_pct: float
    momentum_15m_pct: float
    relative_volume: float | None
    recent_relative_volume: float | None
    breakout_pct: float
    range_position: float
    dollar_volume: float
    stale_minutes: float


@dataclass(frozen=True, slots=True)
class ScoreOutput:
    score: float
    stage: str
    signals: list[str]
    risks: list[str]


def _volume_score(value: float | None, maximum: float) -> float:
    if value is None or value <= 1:
        return 0.0
    return clamp(math.log2(value) * 8.0, 0.0, maximum)


def score_runner(item: ScoreInput) -> ScoreOutput:
    """Score a possible runner from 0 to 100.

    The score favors unusual volume and short-term acceleration. It also rewards
    a move through yesterday's high, then cuts the score when the quote is stale
    or the stock is already heavily extended.
    """

    volume = _volume_score(item.relative_volume, 24.0)
    recent_volume = _volume_score(item.recent_relative_volume, 12.0)
    momentum = clamp(max(item.momentum_5m_pct, 0) * 2.2, 0, 12)
    momentum += clamp(max(item.momentum_15m_pct, 0) * 1.1, 0, 13)

    if item.change_pct <= 0:
        move = 0.0
    elif item.change_pct <= 12:
        move = clamp(item.change_pct * 1.15, 0, 14)
    else:
        move = clamp(14 - (item.change_pct - 12) * 0.25, 5, 14)

    breakout = 0.0
    if item.breakout_pct >= 0:
        breakout = clamp(5 + item.breakout_pct * 1.25, 0, 13)
    elif item.breakout_pct > -2:
        breakout = clamp(4 + item.breakout_pct * 2, 0, 4)

    strength = clamp((item.range_position - 0.45) * 12, 0, 6)
    liquidity = clamp((math.log10(max(item.dollar_volume, 1)) - 4.5) * 4, 0, 8)

    raw = volume + recent_volume + momentum + move + breakout + strength + liquidity

    extended_penalty = clamp((item.change_pct - 15) * 0.55, 0, 16)
    extended_penalty += clamp((item.momentum_15m_pct - 12) * 0.5, 0, 7)

    if item.stale_minutes <= 5:
        freshness = 1.0
    elif item.stale_minutes <= 15:
        freshness = 0.92
    elif item.stale_minutes <= 30:
        freshness = 0.78
    elif item.stale_minutes <= 60:
        freshness = 0.58
    else:
        freshness = 0.35

    score = round(clamp((raw - extended_penalty) * freshness, 0, 100), 1)

    signals: list[str] = []
    risks: list[str] = []
    if item.relative_volume is not None and item.relative_volume >= 2:
        signals.append(f"{item.relative_volume:.1f}x same-time volume")
    if item.recent_relative_volume is not None and item.recent_relative_volume >= 2:
        signals.append(f"{item.recent_relative_volume:.1f}x recent volume")
    if item.momentum_5m_pct >= 1:
        signals.append(f"+{item.momentum_5m_pct:.1f}% in 5m")
    if item.momentum_15m_pct >= 2:
        signals.append(f"+{item.momentum_15m_pct:.1f}% in 15m")
    if item.breakout_pct >= 0:
        signals.append("above prior high")
    if item.range_position >= 0.8:
        signals.append("near session high")

    if item.dollar_volume < 250_000:
        risks.append("thin dollar volume")
    if item.change_pct >= 15:
        risks.append("already extended")
    if item.momentum_15m_pct >= 12:
        risks.append("parabolic 15m move")
    if item.stale_minutes > 15:
        risks.append(f"quote is about {item.stale_minutes:.0f}m old")
    if item.relative_volume is None:
        risks.append("not enough volume history")

    if item.change_pct >= 20 or item.momentum_15m_pct >= 12:
        stage = "EXTENDED"
    elif score >= 58 and item.change_pct < 8:
        stage = "EARLY"
    elif score >= 45 and item.change_pct < 15:
        stage = "BUILDING"
    elif score >= 45:
        stage = "RUNNING"
    else:
        stage = "WATCH"

    return ScoreOutput(score=score, stage=stage, signals=signals, risks=risks)
