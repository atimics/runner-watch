"""Offline study of attention from frozen market features and future price bars.

Run with ``python -m runner_web.attention_study --help``. Outputs are research
artifacts. The public score continues to use its existing policy.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from runner_web.attention import finite_number, market_activity

EASTERN = ZoneInfo("America/New_York")
SEED = 20260924
TARGET = "absolute_4pct_next_full_60m_v1"
BASE_FEATURES = (
    "abs_change",
    "abs_momentum_5m",
    "abs_momentum_15m",
    "abs_previous_5m",
    "volatility",
    "log_rvol",
    "log_recent_rvol",
    "log_dollars",
    "log_recent_dollars",
    "log_average_dollars",
    "quote_age",
    "minute_of_session",
)
CONTEXT_FEATURES = (
    "price_surprise",
    "volume_acceleration",
    "momentum_persistence",
    "peer_move_median",
    "peer_volatility_median",
    "peer_rvol_median",
    "relative_move",
    "relative_volatility",
)


def moment(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return stamp.astimezone(UTC) if stamp.tzinfo is not None else None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def index_bars(rows: list[dict[str, Any]]) -> dict[str, dict[datetime, dict[str, Any]]]:
    """Keep the last archived revision of each bar, keyed by its UTC opening time."""
    result: dict[str, dict[datetime, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        stamp, collected = moment(row.get("bar_time")), moment(row.get("last_collected_at"))
        if stamp is None or collected is None:
            continue
        previous = result[row["ticker"]].get(stamp)
        if previous is None or collected > moment(previous["last_collected_at"]):
            result[row["ticker"]][stamp] = row
    return dict(result)


def future_activity(decision: datetime, bars: dict[datetime, dict[str, Any]]) -> dict[str, Any]:
    """A symmetric excursion from the next full bar's open, with complete coverage.

    Twelve consecutive, completed five-minute bars establish either label. This
    keeps positive and negative examples on the same coverage rule.
    """
    entry_at = datetime.fromtimestamp(math.ceil(decision.timestamp() / 300) * 300, UTC)
    end = entry_at + timedelta(minutes=60)
    receipt: dict[str, Any] = {
        "target": None,
        "entry_at": entry_at.isoformat(),
        "label_end_at": end.isoformat(),
    }
    if end.astimezone(EASTERN).time() > time(16):
        return {**receipt, "status": "outside_regular_session"}
    window = []
    for index in range(12):
        stamp = entry_at + timedelta(minutes=5 * index)
        bar = bars.get(stamp)
        if bar is None:
            return {**receipt, "status": "missing_bar"}
        values = [finite_number(bar.get(key)) for key in ("open", "high", "low", "close")]
        if any(value is None or value <= 0 for value in values):
            return {**receipt, "status": "invalid_bar"}
        opening, high, low, close = values
        if high < max(opening, low, close) or low > min(opening, high, close):
            return {**receipt, "status": "invalid_bar"}
        collected = moment(bar.get("last_collected_at"))
        if collected is None or collected < stamp + timedelta(minutes=5):
            return {**receipt, "status": "partial_bar"}
        window.append(bar)
    reference = float(window[0]["open"])
    up = max(float(bar["high"]) for bar in window) / reference - 1
    down = 1 - min(float(bar["low"]) for bar in window) / reference
    return {
        **receipt,
        "target": int(
            max(float(bar["high"]) for bar in window) >= reference * 1.04
            or min(float(bar["low"]) for bar in window) <= reference * 0.96
        ),
        "status": "resolved",
        "entry_price": reference,
        "max_excursion_pct": 100 * max(up, down),
        "last_revision_at": max(moment(bar["last_collected_at"]) for bar in window).isoformat(),
    }


def prepare_rows(
    snapshots: list[dict[str, Any]], bars: dict[str, dict[datetime, dict[str, Any]]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    output, exclusions = [], Counter()
    identities: set[tuple[str, str]] = set()
    for row in snapshots:
        at, quote = moment(row.get("captured_at")), moment(row.get("quote_time"))
        if at is None or quote is None or not row.get("scan_run_id"):
            exclusions["invalid_clock_or_scan"] += 1
            continue
        local = at.astimezone(EASTERN)
        if row.get("session") != "REGULAR" or not time(9, 30) <= local.time() <= time(15):
            exclusions["outside_decision_window"] += 1
            continue
        age = (at - quote).total_seconds() / 60
        if not 0 <= age <= 45:
            exclusions["quote_age_outside_0_45_minutes"] += 1
            continue
        identity = (str(row["scan_run_id"]), str(row["ticker"]))
        if identity in identities:
            raise ValueError("Repeated ticker within a scan")
        identities.add(identity)
        outcome = future_activity(at, bars.get(row["ticker"], {}))
        output.append({**row, **outcome, "stale_minutes": age, "day": local.date().isoformat()})
    return output, dict(exclusions)


def features(rows: list[dict[str, Any]], *, context: bool) -> np.ndarray:
    """Inputs use saved features and peers from the same decision scan."""

    def number(row: dict, name: str, *, absolute: bool = False, log: bool = False) -> float:
        value = finite_number(row.get(name))
        if value is None or (log and value < 0):
            return float("nan")
        return math.log1p(value) if log else abs(value) if absolute else value

    base = []
    for row in rows:
        local = moment(row["captured_at"]).astimezone(EASTERN)
        base.append(
            [
                *(
                    number(row, key, absolute=True)
                    for key in (
                        "change_pct",
                        "momentum_5m_pct",
                        "momentum_15m_pct",
                        "momentum_previous_5m_pct",
                    )
                ),
                number(row, "intraday_volatility_pct", absolute=True),
                *(
                    number(row, key, log=True)
                    for key in (
                        "relative_volume",
                        "recent_relative_volume",
                        "dollar_volume",
                        "recent_dollar_volume",
                        "average_dollar_volume",
                    )
                ),
                number(row, "stale_minutes"),
                local.hour * 60 + local.minute - 570,
            ]
        )
    matrix = np.asarray(base, dtype=float).reshape(len(rows), len(BASE_FEATURES))
    if not context:
        return matrix
    groups: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        groups[row["scan_run_id"]].append(i)
    extra = np.full((len(rows), len(CONTEXT_FEATURES)), np.nan)
    for indices in groups.values():
        if len({moment(rows[i]["captured_at"]) for i in indices}) != 1:
            raise ValueError("Peer features need a single decision time per scan")
        peers = matrix[indices]
        medians = []
        for column in (0, 4, 5):
            valid = peers[:, column][np.isfinite(peers[:, column])]
            medians.append(float(np.median(valid)) if len(valid) else float("nan"))
        for i in indices:
            row, x = rows[i], matrix[i]
            current, previous = (
                number(row, "momentum_5m_pct"),
                number(row, "momentum_previous_5m_pct"),
            )
            extra[i] = [
                x[1] / max(0.1, x[4]) if math.isfinite(x[4]) else float("nan"),
                x[6] - x[5],
                float(current * previous > 0)
                if math.isfinite(current + previous)
                else float("nan"),
                *medians,
                x[0] - medians[0],
                x[4] - medians[1],
            ]
    return np.column_stack((matrix, extra))


def split_days(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    """Fix 60/20/20 trading-date membership before looking at outcomes."""
    days = sorted({row["day"] for row in rows})
    if len(days) < 10:
        raise ValueError("At least ten decision dates are required")
    train_end, validation_end = int(len(days) * 0.6), int(len(days) * 0.8)
    result = {key: [] for key in ("train", "validation", "test")}
    for i, row in enumerate(rows):
        part = (
            "train"
            if row["day"] < days[train_end]
            else ("validation" if row["day"] < days[validation_end] else "test")
        )
        result[part].append(i)
    # Every full outcome window is intraday, so whole-day boundaries purge overlap.
    for earlier, later in (("train", "validation"), ("validation", "test")):
        boundary = min(moment(rows[i]["captured_at"]) for i in result[later])
        result[earlier] = [i for i in result[earlier] if moment(rows[i]["label_end_at"]) < boundary]
    return result


def fit_tree(matrix: np.ndarray, rows: list[dict[str, Any]], indices: list[int]) -> Any:
    import lightgbm as lgb

    known = [i for i in indices if rows[i]["target"] is not None]
    y = np.asarray([rows[i]["target"] for i in known])
    if len(known) < 500 or len(set(y)) < 2:
        raise ValueError("Training needs 500 complete windows and both outcomes")
    # Equal date weight limits the effect of more frequent scans on a single day.
    counts = Counter(rows[i]["day"] for i in known)
    weights = np.asarray([1 / counts[rows[i]["day"]] for i in known])
    weights *= len(weights) / weights.sum()
    return lgb.train(
        {
            "objective": "binary",
            "num_leaves": 7,
            "max_depth": 3,
            "min_data_in_leaf": 100,
            "learning_rate": 0.05,
            "lambda_l2": 5.0,
            "seed": SEED,
            "num_threads": 1,
            "deterministic": True,
            "force_col_wise": True,
            "verbosity": -1,
        },
        lgb.Dataset(matrix[known], label=y, weight=weights),
        num_boost_round=120,
    )


def _top_indices(
    rows: list[dict[str, Any]], scores: np.ndarray, group: list[int], k: int
) -> list[int]:
    return sorted(
        group,
        key=lambda i: (
            -scores[i],
            finite_number(rows[i].get("baseline_rank")) or 1_000_000,
            rows[i]["ticker"],
        ),
    )[:k]


def rank_metrics(
    rows: list[dict[str, Any]], scores: np.ndarray, indices: list[int], *, k: int = 10
) -> dict[str, Any]:
    """Keep unresolved rows in the selected review budget and report bounds."""
    if k < 1 or len(scores) != len(rows) or not np.isfinite(scores).all():
        raise ValueError("Use finite scores for every row and a positive review budget")
    groups: dict[str, list[int]] = defaultdict(list)
    for i in indices:
        groups[rows[i]["scan_run_id"]].append(i)
    per_day: dict[str, list[dict[str, float]]] = defaultdict(list)
    selected, hits, known = 0, 0, 0
    for group in groups.values():
        top = _top_indices(rows, scores, group, k)
        n, observed = len(top), sum(rows[i]["target"] is not None for i in top)
        found = sum(rows[i]["target"] == 1 for i in top)
        selected, hits, known = selected + n, hits + found, known + observed
        per_day[rows[top[0]]["day"]].append(
            {
                "lower": found / n,
                "upper": (found + n - observed) / n,
                "coverage": observed / n,
            }
        )
    daily = {
        day: {
            key: float(np.mean([scan[key] for scan in scans]))
            for key in ("lower", "upper", "coverage")
        }
        for day, scans in sorted(per_day.items())
    }
    return {
        "scans": len(groups),
        "days": len(daily),
        "selected": selected,
        "observed_hits": hits,
        "selected_resolved": known,
        "precision_given_resolved": hits / known if known else None,
        "coverage": known / selected if selected else None,
        "daily_precision_lower": float(np.mean([d["lower"] for d in daily.values()])),
        "daily_precision_upper": float(np.mean([d["upper"] for d in daily.values()])),
        "daily": daily,
    }


def paired_interval(candidate: dict, baseline: dict) -> dict[str, Any]:
    days = sorted(set(candidate["daily"]) & set(baseline["daily"]))
    difference = np.asarray(
        [candidate["daily"][day]["lower"] - baseline["daily"][day]["lower"] for day in days]
    )
    rng = np.random.default_rng(SEED)
    samples = rng.choice(difference, size=(10000, len(days)), replace=True).mean(axis=1)
    low, high = np.quantile(samples, (0.025, 0.975))
    return {
        "unit": "trading_date",
        "days": len(days),
        "resamples": 10000,
        "mean_difference": float(difference.mean()),
        "ci95": [float(low), float(high)],
    }


def missing_outcome_bounds(
    rows: list[dict[str, Any]],
    candidate: np.ndarray,
    baseline: np.ndarray,
    indices: list[int],
    *,
    k: int = 10,
) -> dict[str, Any]:
    """Bound the paired gain while allowing every unknown outcome to be 0 or 1.

    A row selected by both scores has the same outcome and cancels. Only unknown
    outcomes on the changed review slots widen the bounds.
    """
    groups: dict[str, list[int]] = defaultdict(list)
    for i in indices:
        groups[rows[i]["scan_run_id"]].append(i)
    daily: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for group in groups.values():
        new = set(_top_indices(rows, candidate, group, k))
        old = set(_top_indices(rows, baseline, group, k))
        observed = sum(rows[i]["target"] == 1 for i in new - old) - sum(
            rows[i]["target"] == 1 for i in old - new
        )
        old_unknown = sum(rows[i]["target"] is None for i in old - new)
        new_unknown = sum(rows[i]["target"] is None for i in new - old)
        daily[rows[group[0]]["day"]].append(
            ((observed - old_unknown) / len(new), (observed + new_unknown) / len(new))
        )
    bounds = {
        day: np.asarray(values).mean(axis=0).tolist() for day, values in sorted(daily.items())
    }
    mean = np.asarray(list(bounds.values())).mean(axis=0)
    return {
        "mean_daily_difference_bounds": mean.tolist(),
        "daily": bounds,
        "basis": "shared selections cancel; each changed unknown may be a hit or a miss",
    }


def benchmark(rows: list[dict[str, Any]], output: Path) -> dict[str, Any]:
    split = split_days(rows)
    base = features(rows, context=False)
    contextual = features(rows, context=True)
    scores = {
        "current_market_activity": np.asarray([market_activity(r)["value"] for r in rows]),
        "volatility_only": np.nan_to_num(base[:, 4], nan=0),
    }
    models = {}
    for name, matrix in (("market_trees", base), ("context_trees", contextual)):
        models[name] = fit_tree(matrix, rows, split["train"])
        scores[name] = models[name].predict(matrix, num_threads=1)
    validation = {
        name: rank_metrics(rows, values, split["validation"]) for name, values in scores.items()
    }
    # Pick one trained candidate on validation only; the test stays untouched.
    chosen = max(models, key=lambda name: validation[name]["daily_precision_lower"])
    test = {
        name: rank_metrics(rows, scores[name], split["test"])
        for name in ("current_market_activity", "volatility_only", chosen)
    }
    interval = paired_interval(test[chosen], test["current_market_activity"])
    known_test = [i for i in split["test"] if rows[i]["target"] is not None]
    known_train = [i for i in split["train"] if rows[i]["target"] is not None]
    prior = float(np.mean([rows[i]["target"] for i in known_train]))
    y = np.asarray([rows[i]["target"] for i in known_test])
    p = np.clip(scores[chosen][known_test], 1e-6, 1 - 1e-6)
    output.mkdir(parents=True, exist_ok=True)
    model_path = output / "attention-candidate.txt"
    models[chosen].save_model(str(model_path))
    with gzip.open(output / "scored-rows.jsonl.gz", "wt") as stream:
        for part, indices in split.items():
            for i in indices:
                r = rows[i]
                stream.write(
                    json.dumps(
                        {
                            "snapshot_id": r["snapshot_id"],
                            "scan_run_id": r["scan_run_id"],
                            "ticker": r["ticker"],
                            "day": r["day"],
                            "partition": part,
                            "target": r["target"],
                            "target_status": r["status"],
                            "entry_at": r["entry_at"],
                            "label_end_at": r["label_end_at"],
                            "baseline": float(scores["current_market_activity"][i]),
                            "candidate": float(scores[chosen][i]),
                        }
                    )
                    + "\n"
                )
    return {
        "schema": "attention-study.v1",
        "target": TARGET,
        "seed": SEED,
        "scope": "regular session; quote age 0-45m; decisions 09:30-15:00 New York",
        "split": {
            key: {
                "rows": len(indices),
                "dates": sorted({rows[i]["day"] for i in indices}),
                "resolved": sum(rows[i]["target"] is not None for i in indices),
            }
            for key, indices in split.items()
        },
        "target_coverage": dict(Counter(row["status"] for row in rows)),
        "selection": "highest validation mean daily observed hits per ten review slots",
        "chosen": chosen,
        "validation": validation,
        "test": test,
        "paired_difference": interval,
        "missing_outcome_difference": missing_outcome_bounds(
            rows, scores[chosen], scores["current_market_activity"], split["test"]
        ),
        "probability_check": {
            "resolved_test_rows": len(known_test),
            "train_base_rate": prior,
            "test_base_rate": float(y.mean()),
            "brier": float(np.mean((p - y) ** 2)),
            "constant_train_prior_brier": float(np.mean((prior - y) ** 2)),
            "log_loss": float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))),
        },
        "feature_gain": dict(
            zip(
                BASE_FEATURES + (CONTEXT_FEATURES if chosen == "context_trees" else ()),
                models[chosen].feature_importance(importance_type="gain").tolist(),
                strict=True,
            )
        ),
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "assessment": "retrospective_candidate" if interval["ci95"][0] > 0 else "inconclusive",
        "limitations": [
            "Archived bars use their latest revision; original revision history is unavailable.",
            "Outcome coverage is conditional on twelve complete bars; bounds retain missing rows.",
            "The comparison covers the market component within the saved scanner universe.",
            "Few held-out dates limit uncertainty estimates and regime coverage.",
            "Calibration, full-board replay and prospective validation remain required.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshots", required=True, type=Path)
    parser.add_argument("--bars", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    snapshots = read_jsonl(args.snapshots)
    rows, exclusions = prepare_rows(snapshots, index_bars(read_jsonl(args.bars)))
    result = benchmark(rows, args.output)
    result["input_receipt"] = {
        "snapshot_rows": len(snapshots),
        "included_rows": len(rows),
        "exclusions": exclusions,
        "snapshots_sha256": hashlib.sha256(args.snapshots.read_bytes()).hexdigest(),
        "bars_sha256": hashlib.sha256(args.bars.read_bytes()).hexdigest(),
    }
    (args.output / "report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: result[key] for key in ("chosen", "assessment", "paired_difference")}))


if __name__ == "__main__":
    main()
