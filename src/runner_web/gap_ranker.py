"""A tiny neural ranker for the next bar, so the dashed gap line can lean.

The gap projection used to carry the anchor price forward flat. This trains a
small network on saved five-minute bars that reads price (and volume) alone and
answers one number: how the next bar is likely to move, scaled by what a normal
move for that name has been lately, in [-1, 1]. The projection leans that far
toward the band edge, so the dashed line visibly does something while staying
inside its own uncertainty.

It is deliberately small, deterministic, and easy to refuse: a model is only
promoted to active if it beats always guessing the commoner direction on held
out bars. Otherwise the line stays flat.
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from runner_web.database import DatabaseConnection
from runner_web.db import connection

FEATURE_SCHEMA = "stonks.gap_ranker_features.v1"
MODEL_KIND = "numpy_mlp_tanh_v1"
FEATURE_NAMES = (
    "return_1",
    "return_2",
    "return_3",
    "return_4",
    "return_5",
    "return_6",
    "return_7",
    "return_8",
    "volatility",
    "volume_ratio",
    "range_position",
    "trend",
    "run_length",
    "session_position",
    "midday",
)
RETURN_WINDOW = 8
RANGE_WINDOW = 12
HIDDEN_UNITS = int(os.getenv("GAP_RANKER_HIDDEN_UNITS", "16"))
EPOCHS = int(os.getenv("GAP_RANKER_EPOCHS", "60"))
LEARNING_RATE = float(os.getenv("GAP_RANKER_LEARNING_RATE", "0.02"))
MIN_SAMPLES = int(os.getenv("GAP_RANKER_MIN_SAMPLES", "400"))
MIN_SIGN_ACCURACY = float(os.getenv("GAP_RANKER_MIN_SIGN_ACCURACY", "0.52"))
MAX_TICKERS = int(os.getenv("GAP_RANKER_MAX_TICKERS", "400"))
BARS_PER_TICKER = int(os.getenv("GAP_RANKER_BARS_PER_TICKER", "400"))
TRAIN_INTERVAL_SECONDS = max(
    3600, int(os.getenv("GAP_RANKER_TRAIN_INTERVAL_SECONDS", str(6 * 3600)))
)
SEED = 20260920


def _sigmoid(value: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(value, -30, 30)))


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _log_returns(closes: list[float]) -> list[float]:
    return [
        math.log(later / earlier)
        for earlier, later in zip(closes, closes[1:], strict=False)
        if earlier and later and earlier > 0 and later > 0
    ]


def _trailing_volatility(returns: list[float], *, floor: float = 1e-4) -> float:
    if not returns:
        return floor
    window = returns[-RANGE_WINDOW:]
    mean = sum(window) / len(window)
    variance = sum((value - mean) ** 2 for value in window) / len(window)
    return max(floor, math.sqrt(variance))


def feature_vector(
    closes: list[float],
    volumes: list[float],
    *,
    session_position: float = 0.5,
) -> list[float] | None:
    """Bounded features from price and volume alone, or None when too thin."""

    if len(closes) < RETURN_WINDOW + RANGE_WINDOW + 1:
        return None
    returns = _log_returns(closes[-(RETURN_WINDOW + RANGE_WINDOW + 1) :])
    if len(returns) < RANGE_WINDOW:
        return None
    volatility = _trailing_volatility(returns)
    recent = returns[-RETURN_WINDOW:]
    scaled = [max(-4.0, min(4.0, value / (volatility * math.sqrt(3)))) for value in recent]
    features = [value / 4.0 for value in scaled]
    features.append(min(4.0, math.log1p(volatility * 1000)) / 4.0)
    recent_volumes = volumes[-(RANGE_WINDOW + 1) :]
    if len(recent_volumes) >= 2:
        mean_volume = sum(max(0.0, value) for value in recent_volumes[:-1]) / max(
            1, len(recent_volumes) - 1
        )
        ratio = max(0.0, recent_volumes[-1]) / mean_volume if mean_volume > 0 else 1.0
    else:
        ratio = 1.0
    features.append(_clip(math.log1p(max(0.0, ratio - 0.5)), -1.0, 2.0) / 2.0)
    window = closes[-RANGE_WINDOW:]
    span = max(window) - min(window)
    features.append(_clip((closes[-1] - min(window)) / span * 2 - 1) if span > 0 else 0.0)
    features.append(_clip(sum(recent[:4]) / (volatility * 2), -1.0, 1.0))
    run = 0
    for value in reversed(recent):
        if abs(value) < volatility:
            break
        if (value > 0) != (recent[-1] > 0):
            break
        run += 1
    features.append(_clip(run / RETURN_WINDOW * 2, 0.0, 1.0))
    features.append(_clip(float(session_position)))
    features.append(abs(_clip(float(session_position) * 2 - 1)))
    return [round(value, 6) for value in features]


def _target(returns: list[float], index: int) -> float | None:
    """The next step's move, sized against recent volatility, in [-1, 1]."""

    if index + 1 >= len(returns):
        return None
    history = returns[max(0, index - RANGE_WINDOW) : index + 1]
    volatility = _trailing_volatility(history)
    return _clip(returns[index + 1] / (volatility * math.sqrt(3)))


def build_examples(
    database: DatabaseConnection,
    *,
    max_tickers: int = MAX_TICKERS,
    bars_per_ticker: int = BARS_PER_TICKER,
) -> tuple[np.ndarray, np.ndarray]:
    """Supervised rows from saved bars: features now, movement next."""

    rows = database.execute(
        """
        SELECT ticker, MAX(bar_time) AS latest, COUNT(*) AS bars
        FROM market_bars
        WHERE source='yahoo' AND interval='5m' AND close IS NOT NULL
        GROUP BY ticker ORDER BY latest DESC LIMIT ?
        """,
        (max(1, max_tickers),),
    ).fetchall()
    features: list[list[float]] = []
    targets: list[float] = []
    for row in rows:
        ticker = str(row["ticker"])
        bars = database.execute(
            """
            SELECT close, volume FROM market_bars
            WHERE source='yahoo' AND interval='5m' AND ticker=? AND close IS NOT NULL
            ORDER BY bar_time DESC LIMIT ?
            """,
            (ticker, max(1, bars_per_ticker)),
        ).fetchall()
        bars = list(reversed(bars))
        closes = [float(bar["close"]) for bar in bars]
        volumes = [float(bar["volume"] or 0) for bar in bars]
        if len(closes) < RETURN_WINDOW + RANGE_WINDOW + 2:
            continue
        returns = _log_returns(closes)
        for index in range(RANGE_WINDOW, len(returns) - 1):
            target = _target(returns, index)
            if target is None:
                continue
            vector = feature_vector(
                closes[: index + 2],
                volumes[: index + 2],
                session_position=0.5,
            )
            if vector is None:
                continue
            features.append(vector)
            targets.append(target)
    if not features:
        return np.zeros((0, len(FEATURE_NAMES))), np.zeros((0,))
    return np.asarray(features, dtype=np.float64), np.asarray(targets, dtype=np.float64)


def _init_weights(rng: np.random.Generator, inputs: int) -> dict[str, np.ndarray]:
    return {
        "w1": rng.normal(0.0, 1.0 / math.sqrt(inputs), (inputs, HIDDEN_UNITS)),
        "b1": np.zeros(HIDDEN_UNITS),
        "w2": rng.normal(0.0, 1.0 / math.sqrt(HIDDEN_UNITS), (HIDDEN_UNITS,)),
        "b2": np.zeros(1),
    }


def _forward(weights: dict[str, np.ndarray], x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    hidden = np.tanh(x @ weights["w1"] + weights["b1"])
    output = np.tanh(hidden @ weights["w2"] + weights["b2"])
    return hidden, output


def train(
    features: np.ndarray,
    targets: np.ndarray,
    *,
    epochs: int = EPOCHS,
    learning_rate: float = LEARNING_RATE,
    seed: int = SEED,
) -> dict[str, Any]:
    """Fit the network on a chronological split and report honest metrics."""

    if len(features) < MIN_SAMPLES:
        return {"status": "insufficient", "samples": int(len(features))}
    split = max(1, int(len(features) * 0.8))
    train_x, train_y = features[:split], targets[:split]
    test_x, test_y = features[split:], targets[split:]
    if len(test_x) == 0:
        return {"status": "insufficient", "samples": int(len(features))}
    rng = np.random.default_rng(seed)
    weights = _init_weights(rng, features.shape[1])
    momentum = {key: np.zeros_like(value) for key, value in weights.items()}
    for _ in range(max(1, epochs)):
        hidden, output = _forward(weights, train_x)
        error = (output - train_y) * (1 - output**2)
        grad_w2 = hidden.T @ error / len(train_x)
        grad_b2 = np.asarray([error.mean()])
        hidden_grad = np.outer(error, weights["w2"]) * (1 - hidden**2)
        grad_w1 = train_x.T @ hidden_grad / len(train_x)
        grad_b1 = hidden_grad.mean(axis=0)
        for key, grad in (
            ("w1", grad_w1),
            ("b1", grad_b1),
            ("w2", grad_w2),
            ("b2", grad_b2),
        ):
            momentum[key] = 0.9 * momentum[key] - learning_rate * grad
            weights[key] = weights[key] + momentum[key]
    evaluation = evaluate(weights, test_x, test_y)
    return {
        "status": "trained",
        "samples": int(len(features)),
        "test_samples": int(len(test_x)),
        "metrics": evaluation,
        "weights": weights,
    }


def evaluate(
    weights: dict[str, np.ndarray], features: np.ndarray, targets: np.ndarray
) -> dict[str, Any]:
    _, output = _forward(weights, features)
    errors = output - targets
    sign_matches = int(np.sum(np.sign(output) == np.sign(targets)))
    baseline = max(float(np.mean(targets > 0)), float(np.mean(targets < 0)))
    return {
        "mse": round(float(np.mean(errors**2)), 6),
        "sign_accuracy": round(sign_matches / max(1, len(targets)), 4),
        "baseline_sign_accuracy": round(baseline, 4),
        "mean_prediction": round(float(np.mean(output)), 6),
        "samples": int(len(targets)),
    }


def _serialize(weights: dict[str, np.ndarray]) -> str:
    payload = {
        key: np.asarray(value, dtype=np.float64).round(6).tolist()
        for key, value in weights.items()
    }
    return json.dumps(payload, separators=(",", ":"))


def _deserialize(payload: str) -> dict[str, np.ndarray]:
    raw = json.loads(payload)
    return {key: np.asarray(value, dtype=np.float64) for key, value in raw.items()}


def direction_for_model(model: dict[str, Any] | None, features: list[float] | None) -> float:
    """The lean the dashed line should take, in [-1, 1]; 0.0 means flat."""

    if not model or not features:
        return 0.0
    weights = model.get("weights")
    if not weights:
        return 0.0
    _, output = _forward(weights, np.asarray([features], dtype=np.float64))
    return _clip(float(output[0]))


def train_and_store(database: DatabaseConnection | None = None) -> dict[str, Any]:
    """Train, then promote only when the model beats guessing the common class."""

    moment = datetime.now(UTC)
    if database is not None:
        return _train_and_store(database, moment)
    with connection() as db:
        return _train_and_store(db, moment)


def _train_and_store(database: DatabaseConnection, moment: datetime) -> dict[str, Any]:
    features, targets = build_examples(database)
    result = train(features, targets)
    if result.get("status") != "trained":
        return {key: value for key, value in result.items() if key != "weights"}
    metrics = result["metrics"]
    promoted = (
        metrics["samples"] >= MIN_SAMPLES
        and metrics["sign_accuracy"] >= max(MIN_SIGN_ACCURACY, metrics["baseline_sign_accuracy"])
    )
    model_id = f"gap-{moment.strftime('%Y%m%dT%H%M%S')}"
    database.execute(
        """
        INSERT INTO gap_ranker_models(
            id,feature_schema_version,model_kind,weights_json,metrics_json,
            training_start,training_end,training_rows,status,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            model_id,
            FEATURE_SCHEMA,
            MODEL_KIND,
            _serialize(result["weights"]),
            json.dumps(metrics, separators=(",", ":")),
            _iso(moment - timedelta(days=7)),
            _iso(moment),
            int(result["samples"]),
            "active" if promoted else "shadow",
            _iso(moment),
        ),
    )
    if promoted:
        database.execute(
            """
            UPDATE gap_ranker_models SET status='retired'
            WHERE status='active' AND id<>?
            """,
            (model_id,),
        )
    return {
        "status": "trained",
        "model_id": model_id,
        "promoted": promoted,
        "samples": int(result["samples"]),
        "metrics": metrics,
    }


_ACTIVE_CACHE: tuple[float, dict[str, Any] | None] | None = None
_ACTIVE_TTL_SECONDS = 300.0


def active_model(*, at: datetime | None = None, refresh: bool = False) -> dict[str, Any] | None:
    """The newest promoted model, cached briefly so the chart stays cheap."""

    global _ACTIVE_CACHE
    moment = datetime.now(UTC) if at is None else at
    stamp = moment.timestamp()
    if not refresh and _ACTIVE_CACHE and stamp - _ACTIVE_CACHE[0] < _ACTIVE_TTL_SECONDS:
        return _ACTIVE_CACHE[1]
    model: dict[str, Any] | None = None
    try:
        with connection() as database:
            row = database.execute(
                """
                SELECT id,weights_json,metrics_json,created_at FROM gap_ranker_models
                WHERE status='active' AND feature_schema_version=? AND model_kind=?
                ORDER BY created_at DESC LIMIT 1
                """,
                (FEATURE_SCHEMA, MODEL_KIND),
            ).fetchone()
        if row:
            model = {
                "id": str(row["id"]),
                "weights": _deserialize(str(row["weights_json"])),
                "metrics": json.loads(str(row["metrics_json"])),
                "created_at": str(row["created_at"]),
            }
    except Exception:
        model = None
    _ACTIVE_CACHE = (stamp, model)
    return model


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def main(argv: list[str] | None = None) -> int:
    """Console entry point: train now and report what happened."""

    import argparse

    parser = argparse.ArgumentParser(description="Train the gap ranker from saved bars")
    parser.add_argument("command", nargs="?", default="train", choices=("train", "metrics"))
    args = parser.parse_args(argv)
    if args.command == "metrics":
        model = active_model(refresh=True)
        print(json.dumps(model["metrics"] if model else {"status": "no active model"}))
        return 0
    result = train_and_store()
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result.get("status") == "trained" else 1


if __name__ == "__main__":
    raise SystemExit(main())