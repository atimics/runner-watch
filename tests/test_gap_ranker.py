"""The next-bar ranker behind the dashed gap line.

It reads price and volume alone, answers one number in [-1, 1], and is only
promoted to active when it beats guessing the commoner direction on held-out
bars. These tests pin the features, the honest promotion rule, and the promise
that a leaned projection never leaves its own band.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from runner_web import db, gap_ranker, price_gap

UTC_TUESDAY = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "gap-ranker.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(gap_ranker, "_ACTIVE_CACHE", None)
    db.init_db()
    with db.connection() as connection:
        yield connection


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def add_bars(
    connection, ticker: str, prices: list[float], *, volumes: list[float] | None = None
) -> None:
    volumes = volumes or [1_000.0] * len(prices)
    with connection as conn:
        conn.executemany(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,open,high,low,close,volume,
                first_collected_at,last_collected_at
            ) VALUES('yahoo',?, '5m',?,?,?,?,?,?,?,?)
            """,
            [
                (
                    ticker,
                    _iso(UTC_TUESDAY + timedelta(minutes=5 * index)),
                    price,
                    price,
                    price,
                    price,
                    volumes[index],
                    _iso(UTC_TUESDAY),
                    _iso(UTC_TUESDAY),
                )
                for index, price in enumerate(prices)
            ],
        )


def test_features_are_bounded_and_repeatable():
    closes = [100 * (1.001**index) for index in range(40)]
    volumes = [1_000 + index * 10 for index in range(40)]
    first = gap_ranker.feature_vector(closes, volumes, session_position=0.4)
    second = gap_ranker.feature_vector(closes, volumes, session_position=0.4)
    assert first == second
    assert first is not None and len(first) == len(gap_ranker.FEATURE_NAMES)
    assert all(-1.0 <= value <= 1.0 for value in first)


def test_thin_history_has_no_features():
    assert gap_ranker.feature_vector([1.0] * 5, [1.0] * 5) is None


def test_targets_are_sized_by_volatility_and_clipped():
    returns = [0.001, -0.001, 0.002, -0.002, 0.05]
    target = gap_ranker._target(returns, 3)
    assert target is not None and -1.0 <= target <= 1.0
    assert target > 0.9  # a move far past recent volatility pins the value
    assert gap_ranker._target(returns, len(returns) - 1) is None


def test_training_learns_a_signal_and_beats_the_baseline():
    rng = np.random.default_rng(7)
    rows = 2_000
    signal = rng.normal(0, 1, rows)
    features = np.column_stack([signal, rng.normal(0, 0.2, rows)])
    targets = np.clip(signal + rng.normal(0, 0.3, rows), -1, 1)
    result = gap_ranker.train(features, targets, epochs=40)
    assert result["status"] == "trained"
    metrics = result["metrics"]
    assert metrics["sign_accuracy"] > metrics["baseline_sign_accuracy"]
    prediction = gap_ranker.direction_for_model(
        {"weights": result["weights"]}, [1.0, 0.0]
    )
    assert 0.5 < prediction <= 1.0


def test_training_refuses_too_few_examples(monkeypatch):
    monkeypatch.setattr(gap_ranker, "MIN_SAMPLES", 100)
    result = gap_ranker.train(np.zeros((10, 2)), np.zeros(10))
    assert result == {"status": "insufficient", "samples": 10}


def test_a_model_that_beats_the_baseline_is_promoted(database, monkeypatch):
    monkeypatch.setattr(gap_ranker, "MIN_SAMPLES", 50)
    # A momentum tape: each move partly continues, so the last return carries
    # real information about the next one.
    rng = np.random.default_rng(3)
    prices = [100.0]
    previous = 0.0
    for _ in range(400):
        previous = 0.7 * previous + float(rng.normal(0, 0.0006))
        prices.append(prices[-1] * float(np.exp(previous)))
    add_bars(database, "MOMO", prices)

    result = gap_ranker.train_and_store(database)
    database.commit()  # the promotion is visible to other connections

    assert result["status"] == "trained"
    metrics = result["metrics"]
    assert metrics["samples"] >= 50
    model = gap_ranker.active_model(refresh=True)
    assert model is not None
    assert model["metrics"]["sign_accuracy"] >= model["metrics"]["baseline_sign_accuracy"]


def test_noise_keeps_the_model_in_shadow(database, monkeypatch):
    monkeypatch.setattr(gap_ranker, "MIN_SAMPLES", 50)
    monkeypatch.setattr(gap_ranker, "MIN_SIGN_ACCURACY", 0.9)
    rng = np.random.default_rng(11)
    prices = list(100 + np.cumsum(rng.normal(0, 0.1, 400)))
    add_bars(database, "NOISE", prices)

    result = gap_ranker.train_and_store(database)
    database.commit()

    assert result["status"] == "trained"
    assert result["promoted"] is False
    assert gap_ranker.active_model(refresh=True) is None
    with database as conn:
        status = conn.execute("SELECT status FROM gap_ranker_models").fetchone()["status"]
    assert status == "shadow"


def _store_always_up_model(database) -> None:
    """A model whose answer is +1 whatever the features say."""

    weights = {
        "w1": np.zeros((len(gap_ranker.FEATURE_NAMES), gap_ranker.HIDDEN_UNITS)).tolist(),
        "b1": np.zeros(gap_ranker.HIDDEN_UNITS).tolist(),
        "w2": np.zeros(gap_ranker.HIDDEN_UNITS).tolist(),
        "b2": [6.0],
    }
    with database as conn:
        conn.execute(
            """
            INSERT INTO gap_ranker_models(
                id,feature_schema_version,model_kind,weights_json,metrics_json,
                training_start,training_end,training_rows,status,created_at
            ) VALUES('gap-test',?,?,?,?,?,?,?, 'active',?)
            """,
            (
                gap_ranker.FEATURE_SCHEMA,
                gap_ranker.MODEL_KIND,
                json.dumps(weights, separators=(",", ":")),
                json.dumps({"sign_accuracy": 0.6}, separators=(",", ":")),
                _iso(UTC_TUESDAY),
                _iso(UTC_TUESDAY),
                1000,
                _iso(UTC_TUESDAY),
            ),
        )


def test_the_dashed_line_leans_inside_its_band(database):
    add_bars(database, "LEAN", [100 + index * 0.1 for index in range(60)])
    _store_always_up_model(database)
    assert gap_ranker.active_model(refresh=True) is not None

    projection = price_gap.gap_projection(database, "LEAN", at=UTC_TUESDAY + timedelta(hours=8))

    assert projection is not None
    assert projection["direction"] > 0.9
    assert projection["model_version"] == "gap-test"
    assert len(projection["path"]) >= 2
    for point in projection["path"]:
        # Leaning up, but the band is still the bound.
        assert point["low"] <= point["price"] <= point["high"]
        assert point["price"] > projection["anchor_price"]
    assert projection["path"][-1]["price"] <= projection["path"][-1]["high"]


def test_without_a_model_the_projection_stays_flat(database):
    add_bars(database, "FLAT", [100 + index * 0.1 for index in range(60)])

    projection = price_gap.gap_projection(database, "FLAT", at=UTC_TUESDAY + timedelta(hours=8))

    assert projection is not None
    assert projection["direction"] == 0.0
    assert all(point["price"] == projection["anchor_price"] for point in projection["path"])
    assert all(point["low"] <= point["price"] <= point["high"] for point in projection["path"])


def test_recorded_forecasts_name_the_model_that_made_them(database):
    add_bars(database, "LEAN", [100 + index * 0.1 for index in range(60)])
    _store_always_up_model(database)
    gap_ranker.active_model(refresh=True)

    at = UTC_TUESDAY + timedelta(hours=8)
    assert price_gap.record_forecasts(database, tickers=["LEAN"], at=at) == 1

    with database as conn:
        row = conn.execute(
            "SELECT model_version,predicted_path_json FROM price_gap_forecasts"
        ).fetchone()
    assert row["model_version"] == "gap-test"
    predicted = json.loads(row["predicted_path_json"])
    assert predicted[-1]["price"] > predicted[0]["price"]