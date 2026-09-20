"""Record what price was most likely doing while the bars were missing.

Five-minute bars land every five minutes while a market is open, so a chart's
last point is always a few minutes behind the clock. Most charts imply the last
candle is *now*; RATi should say what it knows and show the rest as a
projection. This module records that projection -- "fill in this gap" -- and,
once the missing bars arrive, scores it against what actually printed. Those
(prediction, outcome) pairs are the training signal for the model that will
eventually fill the gap properly.

The first model is deliberately simple and deterministic: carry the anchor
price forward with a band from realised five-minute volatility, widened by the
square root of the gap. It never invents a direction it cannot support, and the
same anchor always produces the same forecast, so every request and instance
agrees.
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web import gap_ranker
from runner_web.database import DatabaseConnection
from runner_web.db import connection
from runner_web.market_clock import market_clock

MODEL_VERSION = "carry-volatility-v1"
STEP_MINUTES = max(1, int(os.getenv("PRICE_GAP_STEP_MINUTES", "5")))
MAX_HORIZON_MINUTES = max(STEP_MINUTES, int(os.getenv("PRICE_GAP_MAX_HORIZON_MINUTES", "120")))
# A forecast only exists once a bar is actually late, so every row describes a
# real gap rather than the ordinary few seconds between a bar and the clock.
MIN_GAP_SECONDS = max(5, int(os.getenv("PRICE_GAP_MIN_GAP_SECONDS", "300")))
SAMPLE_BARS = max(12, int(os.getenv("PRICE_GAP_SAMPLE_BARS", "48")))
TRACKED_TICKERS = max(1, int(os.getenv("PRICE_GAP_TICKER_LIMIT", "300")))
# Per-step log-return floor, so a quiet tape still shows a band rather than a line.
VOLATILITY_FLOOR = max(0.0, float(os.getenv("PRICE_GAP_VOLATILITY_FLOOR", "0.0002")))
EWMA_LAMBDA = 0.94
BAND_SIGMA = 1.0
# How far toward the band edge the centre of a leaned projection may travel.
LEAN = max(0.0, min(1.0, float(os.getenv("PRICE_GAP_LEAN", "0.6"))))


def _utc(value: datetime | None = None) -> datetime:
    moment = value or datetime.now(UTC)
    return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).isoformat()


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def step_volatility(returns: list[float], *, floor: float = VOLATILITY_FLOOR) -> float:
    """EWMA volatility of per-step log returns, floored so a band always exists."""

    values = [value for value in returns if math.isfinite(value)]
    if not values:
        return floor
    variance = sum(value * value for value in values) / len(values)
    for value in values:
        variance = EWMA_LAMBDA * variance + (1 - EWMA_LAMBDA) * value * value
    return max(floor, math.sqrt(max(0.0, variance)))


def forecast_path(
    anchor_price: float,
    volatility: float,
    horizon_minutes: int,
    *,
    step_minutes: int = STEP_MINUTES,
    sigma: float = BAND_SIGMA,
    direction: float = 0.0,
    lean: float = LEAN,
) -> list[dict[str, float]]:
    """Carry the anchor forward inside a band that widens by sqrt(steps).

    A direction in [-1, 1] leans the carried price toward the edge the ranker
    favours, but never past it: the band stays the uncertainty and the lean only
    says which way the middle of it sits.
    """

    if anchor_price <= 0:
        return []
    steps = max(1, math.ceil(max(1, horizon_minutes) / step_minutes))
    lean = max(0.0, min(1.0, lean))
    direction = max(-1.0, min(1.0, direction))
    path: list[dict[str, float]] = []
    for step in range(1, steps + 1):
        reach = math.exp(sigma * volatility * math.sqrt(step))
        travel = reach - 1.0 if direction >= 0 else 1.0 - 1.0 / reach
        price = anchor_price * (1.0 + direction * lean * travel)
        path.append(
            {
                "step": step,
                "price": round(price, 6),
                "low": round(anchor_price / reach, 6),
                "high": round(anchor_price * reach, 6),
            }
        )
    return path


def gap_state(latency_seconds: float, *, session: str, scanner_active: bool) -> str:
    """live within a cadence, lagging past it, stale when bars should have caught up."""

    if not scanner_active:
        return "closed"
    cadence = STEP_MINUTES * 60
    if latency_seconds <= cadence + 90:
        return "live"
    if latency_seconds <= cadence * 3:
        return "lagging"
    return "stale"


def recent_bars(
    database: DatabaseConnection, ticker: str, *, limit: int = SAMPLE_BARS
) -> list[dict[str, Any]]:
    rows = database.execute(
        """
        SELECT bar_time,open,high,low,close,volume
        FROM market_bars
        WHERE source='yahoo' AND interval='5m' AND ticker=? AND close IS NOT NULL
        ORDER BY bar_time DESC LIMIT ?
        """,
        (ticker, max(2, limit)),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def anchor_for(database: DatabaseConnection, ticker: str) -> dict[str, Any] | None:
    """The newest evidence of price: a saved bar or, when fresher, a live quote."""

    bar = database.execute(
        """
        SELECT bar_time AS time, close AS price
        FROM market_bars
        WHERE source='yahoo' AND interval='5m' AND ticker=? AND close IS NOT NULL
        ORDER BY bar_time DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    quote = database.execute(
        """
        SELECT observed_at AS time, price
        FROM ticker_quotes
        WHERE ticker=? AND status='ok' AND price IS NOT NULL AND observed_at IS NOT NULL
        ORDER BY observed_at DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for kind, row in (("bar", bar), ("quote", quote)):
        if not row:
            continue
        moment = _parse(row["time"])
        price = _number(row["price"])
        if moment and price and price > 0:
            candidates.append((moment, {"kind": kind, "time": moment, "price": price}))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _features(
    bars: list[dict[str, Any]],
    *,
    anchor: dict[str, Any],
    latency_seconds: float,
    volatility: float,
    clock: dict[str, Any],
    quote: dict[str, Any] | None,
) -> dict[str, Any]:
    closes = [value for value in (_number(bar["close"]) for bar in bars) if value]
    returns = [
        math.log(later / earlier)
        for earlier, later in zip(closes, closes[1:], strict=False)
        if earlier and later and earlier > 0 and later > 0
    ]
    volumes = [value for value in (_number(bar["volume"]) for bar in bars) if value is not None]
    mean_volume = sum(volumes) / len(volumes) if volumes else 0.0
    spread_bps = None
    if quote:
        bid, ask = _number(quote.get("bid")), _number(quote.get("ask"))
        if bid and ask and bid > 0 and ask >= bid:
            spread_bps = round((ask - bid) / ((ask + bid) / 2) * 10_000, 4)
    return {
        "bars": {
            "count": len(bars),
            "first": bars[0]["bar_time"] if bars else None,
            "last": bars[-1]["bar_time"] if bars else None,
        },
        "returns": {
            "last": round(returns[-1], 8) if returns else None,
            "mean": round(sum(returns) / len(returns), 8) if returns else None,
            "step_volatility": round(volatility, 8),
        },
        "volume": {
            "last": volumes[-1] if volumes else None,
            "mean": round(mean_volume, 4),
            "ratio": round(volumes[-1] / mean_volume, 4) if volumes and mean_volume else None,
        },
        "gap": {
            "latency_seconds": int(latency_seconds),
            "anchor_kind": anchor["kind"],
            "anchor_time": _iso(anchor["time"]),
        },
        "session": {
            "key": clock.get("session"),
            "label": clock.get("label"),
            "seconds_to_change": clock.get("countdown_seconds"),
        },
        "quote": {
            "age_seconds": int((_utc() - _parse(quote["observed_at"])).total_seconds())
            if quote and _parse(quote.get("observed_at"))
            else None,
            "spread_bps": spread_bps,
        },
    }


def forecast_gap(
    database: DatabaseConnection,
    ticker: str,
    *,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Build the gap forecast for one ticker, or None when there is no gap."""

    moment = _utc(at)
    clock = market_clock(moment)
    anchor = anchor_for(database, ticker)
    if anchor is None:
        return None
    latency_seconds = (moment - anchor["time"]).total_seconds()
    if latency_seconds < MIN_GAP_SECONDS:
        return None
    state = gap_state(
        latency_seconds,
        session=str(clock.get("session")),
        scanner_active=bool(clock.get("scanner_active")),
    )
    # A closed market is still a gap worth predicting: bars stop on purpose and
    # the weekend stretch is exactly where a projection earns its keep. The row
    # keeps the bar-lateness vocabulary and market_open carries the honest word.
    market_open = state != "closed"
    bars = recent_bars(database, ticker)
    closes = [value for value in (_number(bar["close"]) for bar in bars) if value]
    returns = [
        math.log(later / earlier)
        for earlier, later in zip(closes, closes[1:], strict=False)
        if earlier and later and earlier > 0 and later > 0
    ]
    volatility = step_volatility(returns)
    horizon_minutes = min(MAX_HORIZON_MINUTES, max(STEP_MINUTES, math.ceil(latency_seconds / 60)))
    direction, model_id = model_direction(bars, clock)
    path = forecast_path(
        anchor["price"], volatility, horizon_minutes, direction=direction
    )
    if not path:
        return None
    quote = database.execute(
        """
        SELECT bid,ask,observed_at
        FROM security_quotes
        WHERE ticker=? ORDER BY observed_at DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    return {
        "ticker": ticker,
        # The recorded model is whichever produced this path, so the dataset can
        # tell a flat carry from a leaned one later.
        "model_version": model_id or MODEL_VERSION,
        "direction": direction,
        "anchor_time": _iso(anchor["time"]),
        "anchor_kind": anchor["kind"],
        "anchor_price": round(anchor["price"], 6),
        "session": str(clock.get("session")),
        "state": "stale" if not market_open else state,
        "market_open": market_open,
        "latency_seconds": int(latency_seconds),
        "step_minutes": STEP_MINUTES,
        "horizon_minutes": horizon_minutes,
        "predicted_path": [{"step": point["step"], "price": point["price"]} for point in path],
        "band": [
            {"step": point["step"], "low": point["low"], "high": point["high"]}
            for point in path
        ],
        "features": _features(
            bars,
            anchor=anchor,
            latency_seconds=latency_seconds,
            volatility=volatility,
            clock=clock,
            quote=dict(quote) if quote else None,
        ),
    }


def _duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 60 * 24:
        return f"{minutes // 60}h"
    return f"{minutes // (60 * 24)}d"


def gap_projection(
    database: DatabaseConnection,
    ticker: str,
    *,
    at: datetime | None = None,
    max_points: int = 96,
) -> dict[str, Any] | None:
    """The dashed stretch the chart draws: anchor to now, honestly labelled.

    Unlike a stored forecast this reaches all the way to the clock, so a weekend
    gap is drawn as the long flat stretch it is. The band widens with the square
    root of the gap, which is what makes a three-day projection look uncertain
    rather than confident.
    """

    moment = _utc(at)
    anchor = anchor_for(database, ticker)
    if anchor is None:
        return None
    latency_seconds = (moment - anchor["time"]).total_seconds()
    if latency_seconds < MIN_GAP_SECONDS:
        return None
    clock = market_clock(moment)
    market_open = bool(clock.get("scanner_active"))
    bars = recent_bars(database, ticker)
    closes = [value for value in (_number(bar["close"]) for bar in bars) if value]
    returns = [
        math.log(later / earlier)
        for earlier, later in zip(closes, closes[1:], strict=False)
        if earlier and later and earlier > 0 and later > 0
    ]
    volatility = step_volatility(returns)
    direction, model_id = model_direction(bars, clock)
    gap_minutes = max(1, math.ceil(latency_seconds / 60))
    step = max(STEP_MINUTES, math.ceil(gap_minutes / max(2, max_points)))
    steps = max(1, math.ceil(gap_minutes / step))
    path: list[dict[str, Any]] = []
    for index in range(1, steps + 1):
        reach = math.exp(BAND_SIGMA * volatility * math.sqrt(index * step / STEP_MINUTES))
        # The lean peeks toward the band edge the ranker favours, never past it.
        travel = reach - 1.0 if direction >= 0 else 1.0 - 1.0 / reach
        price = anchor["price"] * (1.0 + direction * LEAN * travel)
        path.append(
            {
                "time": _iso(anchor["time"] + timedelta(minutes=index * step)),
                "price": round(price, 6),
                "low": round(anchor["price"] / reach, 6),
                "high": round(anchor["price"] * reach, 6),
            }
        )
    band_pct = round((path[-1]["high"] / anchor["price"] - 1) * 100, 2)
    state = gap_state(
        latency_seconds,
        session=str(clock.get("session")),
        scanner_active=market_open,
    )
    if market_open:
        label = (
            f"No bars for {_duration(gap_minutes)} · "
            f"dashed line is our projection (±{band_pct}%)"
        )
    else:
        label = f"Market closed · dashed line is our flat projection (±{band_pct}%)"
    return {
        "ticker": ticker,
        "model_version": model_id or MODEL_VERSION,
        "direction": round(direction, 4),
        "anchor_time": _iso(anchor["time"]),
        "anchor_kind": anchor["kind"],
        "anchor_price": round(anchor["price"], 6),
        "latency_seconds": int(latency_seconds),
        "gap_minutes": gap_minutes,
        "state": "stale" if not market_open else state,
        "market_open": market_open,
        "session": str(clock.get("session")),
        "session_label": clock.get("label"),
        "step_minutes": step,
        "band_pct": band_pct,
        "label": label,
        "path": path,
    }


_SESSION_HOURS = {"pre": 5.5, "regular": 6.5, "after": 4.0}


def session_position(clock: dict[str, Any]) -> float:
    """How far through the session we are, 0 at the open and 1 at the close."""

    length = _SESSION_HOURS.get(str(clock.get("session")))
    if not length:
        return 0.5
    remaining = float(clock.get("countdown_seconds") or 0.0)
    position = 1.0 - remaining / (length * 3600.0)
    return max(0.0, min(1.0, position))


def model_direction(
    bars: list[dict[str, Any]], clock: dict[str, Any]
) -> tuple[float, str | None]:
    """The ranker's lean for the next bar, or nothing when it has no active model."""

    model = gap_ranker.active_model()
    if not model:
        return 0.0, None
    closes = [value for value in (_number(bar["close"]) for bar in bars) if value]
    volumes = [value or 0.0 for value in (_number(bar["volume"]) for bar in bars)]
    vector = gap_ranker.feature_vector(closes, volumes, session_position=session_position(clock))
    if vector is None:
        return 0.0, None
    return gap_ranker.direction_for_model(model, vector), str(model["id"])


def tracked_tickers(database: DatabaseConnection, *, limit: int = TRACKED_TICKERS) -> list[str]:
    """The scanner's current board, which is where a reader's attention is."""

    rows = database.execute(
        """
        SELECT ticker FROM scan_snapshots
        WHERE scan_run_id=(
            SELECT id FROM scan_runs WHERE candidate_rows>0 ORDER BY captured_at DESC LIMIT 1
        )
        ORDER BY score DESC,baseline_rank,ticker LIMIT ?
        """,
        (max(1, limit),),
    ).fetchall()
    return [str(row["ticker"]) for row in rows]


def record_forecasts(
    database: DatabaseConnection,
    *,
    tickers: list[str] | None = None,
    at: datetime | None = None,
) -> int:
    """Store one forecast per anchor. A saved forecast is never rewritten."""

    moment = _utc(at)
    names = tickers if tickers is not None else tracked_tickers(database)
    recorded = 0
    for ticker in names:
        forecast = forecast_gap(database, str(ticker), at=moment)
        if not forecast:
            continue
        cursor = database.execute(
            """
            INSERT INTO price_gap_forecasts(
                ticker,model_version,anchor_time,anchor_kind,anchor_price,session,state,
                market_open,latency_seconds,step_minutes,horizon_minutes,predicted_path_json,
                band_json,features_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker,model_version,anchor_time) DO NOTHING
            """,
            (
                forecast["ticker"],
                forecast["model_version"],
                forecast["anchor_time"],
                forecast["anchor_kind"],
                forecast["anchor_price"],
                forecast["session"],
                forecast["state"],
                1 if forecast["market_open"] else 0,
                forecast["latency_seconds"],
                forecast["step_minutes"],
                forecast["horizon_minutes"],
                json.dumps(forecast["predicted_path"], separators=(",", ":")),
                json.dumps(forecast["band"], separators=(",", ":")),
                json.dumps(forecast["features"], separators=(",", ":")),
                _iso(moment),
            ),
        )
        recorded += max(0, cursor.rowcount or 0)
    return recorded


def _actual_closes(
    database: DatabaseConnection,
    ticker: str,
    *,
    start: datetime,
    horizon_minutes: int,
) -> dict[str, float]:
    rows = database.execute(
        """
        SELECT bar_time,close FROM market_bars
        WHERE source='yahoo' AND interval='5m' AND ticker=?
          AND bar_time>? AND bar_time<=? AND close IS NOT NULL
        ORDER BY bar_time
        """,
        (ticker, _iso(start), _iso(start + timedelta(minutes=horizon_minutes))),
    ).fetchall()
    closes: dict[str, float] = {}
    for row in rows:
        moment = _parse(row["bar_time"])
        price = _number(row["close"])
        if moment and price:
            closes[_iso(moment)] = price
    return closes


def resolve_forecasts(
    database: DatabaseConnection, *, at: datetime | None = None, limit: int = 500
) -> int:
    """Score forecasts whose gap has been filled by saved bars."""

    moment = _utc(at)
    rows = database.execute(
        """
        SELECT ticker,model_version,anchor_time,anchor_price,step_minutes,horizon_minutes,
               predicted_path_json,band_json
        FROM price_gap_forecasts
        WHERE resolved_at IS NULL AND anchor_time<?
        ORDER BY anchor_time LIMIT ?
        """,
        (_iso(moment - timedelta(minutes=STEP_MINUTES)), max(1, limit)),
    ).fetchall()
    resolved = 0
    for row in rows:
        anchor_time = _parse(row["anchor_time"])
        predicted = json.loads(row["predicted_path_json"] or "[]")
        band = {int(point["step"]): point for point in json.loads(row["band_json"] or "[]")}
        anchor_price = _number(row["anchor_price"])
        if not anchor_time or not predicted or not anchor_price:
            continue
        closes = _actual_closes(
            database,
            str(row["ticker"]),
            start=anchor_time,
            horizon_minutes=int(row["horizon_minutes"]),
        )
        actual: list[dict[str, float]] = []
        for point in predicted:
            step = int(point["step"])
            target = anchor_time + timedelta(minutes=step * int(row["step_minutes"]))
            price = closes.get(_iso(target))
            if price is None:
                actual = []
                break
            actual.append({"step": step, "time": _iso(target), "price": round(price, 6)})
        if not actual:
            continue
        errors = [
            abs(point["price"] - predicted[index]["price"]) / anchor_price
            for index, point in enumerate(actual)
        ]
        covered = all(
            band[int(point["step"])]["low"] <= point["price"] <= band[int(point["step"])]["high"]
            for point in actual
            if int(point["step"]) in band
        )
        database.execute(
            """
            UPDATE price_gap_forecasts
            SET resolved_at=?, actual_path_json=?, abs_error=?, pct_error=?,
                max_pct_error=?, covered=?
            WHERE ticker=? AND model_version=? AND anchor_time=?
            """,
            (
                _iso(moment),
                json.dumps(actual, separators=(",", ":")),
                round(
                    sum(abs(point["price"] - anchor_price) for point in actual) / len(actual),
                    6,
                ),
                round(sum(errors) / len(errors), 8),
                round(max(errors), 8),
                1 if covered else 0,
                row["ticker"],
                row["model_version"],
                row["anchor_time"],
            ),
        )
        resolved += 1
    return resolved


def gap_summary(database: DatabaseConnection) -> dict[str, Any]:
    """Honest scoreboard for the projections: how wrong have we been?"""

    row = database.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
               AVG(pct_error) AS mean_pct_error,
               AVG(CASE WHEN covered=1 THEN 1.0 ELSE 0.0 END) AS coverage
        FROM price_gap_forecasts
        """
    ).fetchone()
    values = dict(row) if row else {}
    return {
        "total": int(values.get("total") or 0),
        "resolved": int(values.get("resolved") or 0),
        "mean_pct_error": _number(values.get("mean_pct_error")),
        "coverage": _number(values.get("coverage")),
    }


def refresh_price_gaps(
    database: DatabaseConnection | None = None,
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    """One capture pass: score what closed, then record what is still open."""

    moment = _utc(at)
    if database is not None:
        return _refresh(database, moment)
    with connection() as db:
        return _refresh(db, moment)


def _refresh(database: DatabaseConnection, moment: datetime) -> dict[str, Any]:
    resolved = resolve_forecasts(database, at=moment)
    # Closed markets are recorded too: the weekend gap is a training example.
    recorded = record_forecasts(database, at=moment)
    return {"recorded": recorded, "resolved": resolved, "summary": gap_summary(database)}
