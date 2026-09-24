"""Prospective attention receipts, price windows, and release evidence.

Inspect with ``python -m runner_web.attention_trial``. The trial records the
frozen research model beside the public board and retains every selected slot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from runner_web import attention
from runner_web.attention_model import artifact, probability
from runner_web.attention_study import (
    EASTERN,
    TARGET,
    features,
    future_activity,
    missing_outcome_bounds,
    moment,
    paired_interval,
    rank_metrics,
)
from runner_web.data_health import _market_window
from runner_web.db import connection

LOG = logging.getLogger(__name__)
CONTRACT = {
    "version": "attention-shadow-v1",
    "target": TARGET,
    "anchor": "next_full_5m_bar_after_predictions_commit",
    "bars": "first_completed_yahoo_5m_observation_per_bar",
    "outcome_grace_hours": 6,
    "session": "XNYS_calendar_including_early_closes",
    "features": "same_scan_eligible_peers_as_of_evidence_time",
    "quote_age_minutes": [0, 45],
    "candidate_market_points": "80_times_raw_model_probability",
    "phase": "calibration_collection",
    "calibration_sessions": 10,
    "later_evaluation_sessions": 20,
    "top_k": 10,
    "minimum_lift": 0.10,
    "minimum_coverage": 0.90,
}
INPUT_KEYS = (
    "id",
    "scan_run_id",
    "captured_at",
    "ticker",
    "session",
    "quote_time",
    "price",
    "baseline_rank",
    "change_pct",
    "momentum_5m_pct",
    "momentum_15m_pct",
    "momentum_previous_5m_pct",
    "intraday_volatility_pct",
    "relative_volume",
    "recent_relative_volume",
    "dollar_volume",
    "recent_dollar_volume",
    "average_dollar_volume",
)


def enabled() -> bool:
    return os.getenv("ATTENTION_SHADOW_ENABLED", "0") == "1"


def utcnow() -> datetime:
    return datetime.now(UTC)


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _window(at: datetime) -> tuple[datetime, datetime] | None:
    session = _market_window(at)
    entry = datetime.fromtimestamp(math.ceil(at.timestamp() / 300) * 300, UTC)
    end = entry + timedelta(hours=1)
    if session and session[0] <= at < session[1] and end <= session[1]:
        return entry, end
    return None


def _reason(row: dict, at: datetime) -> str:
    quote = moment(row.get("quote_time"))
    feature_at = moment(row.get("captured_at"))
    if row.get("session") != "REGULAR":
        return "session_fallback"
    if feature_at is None or feature_at > at:
        return "feature_clock_fallback"
    if quote is None or not 0 <= (at - quote).total_seconds() <= 45 * 60:
        return "quote_clock_fallback"
    if (attention.finite_number(row.get("price")) or 0) <= 0:
        return "price_fallback"
    return "learned"


def capture_scan(scan_run_id: str) -> str:
    """Freeze board evidence and predictions once per scan, with isolated failures."""
    from runner_web.main import _pulse_scoring_inputs, _pulse_snapshot_score

    at = utcnow()
    model = artifact()
    with connection() as db:
        claimed = db.execute(
            """INSERT INTO attention_trial_runs(
                id,scan_run_id,model_id,model_sha256,policy,contract_json,evidence_as_of,
                day,status,expected_rows,build_sha
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scan_run_id) DO NOTHING""",
            (
                scan_run_id,
                scan_run_id,
                model["id"],
                model["sha256"],
                attention.POLICY_VERSION,
                encode(CONTRACT),
                at.isoformat(),
                at.astimezone(EASTERN).date().isoformat(),
                "recording" if _window(at) else "outside_window",
                0,
                os.getenv("APP_BUILD_SHA", "dev"),
            ),
        ).rowcount
    if not claimed:
        return "already_recorded"
    if not _window(at):
        return "outside_window"
    try:
        inputs = _pulse_scoring_inputs(at=at, scan_run_id=scan_run_id)
        rows = inputs["market_rows"]
        if not rows or len({r["ticker"] for r in rows}) != len(rows):
            raise ValueError("Trial needs one row per ticker in a complete scan")
        expected = int(inputs["latest_run"]["candidate_rows"])
        if len(rows) != expected:
            raise ValueError("Trial candidate count differs from the saved scan")
        board = [_pulse_snapshot_score(row, inputs) for row in rows]
        persist_predictions(scan_run_id, rows, board, at)
    except Exception as exc:
        with connection() as db:
            db.execute(
                "UPDATE attention_trial_runs SET status='capture_error',error=? WHERE id=?",
                (type(exc).__name__, scan_run_id),
            )
        LOG.exception("Attention trial capture failed for %s", scan_run_id)
        return "capture_error"
    return "recorded"


def persist_predictions(run_id: str, rows: list[dict], board: list[dict], at: datetime) -> None:
    reasons = [_reason(row, at) for row in rows]
    eligible = [i for i, reason in enumerate(reasons) if reason == "learned"]
    matrix = features(
        [
            {
                **rows[i],
                "captured_at": at.isoformat(),
                "stale_minutes": (at - moment(rows[i]["quote_time"])).total_seconds() / 60,
            }
            for i in eligible
        ],
        context=True,
    )
    vectors = {
        i: [float(v) if math.isfinite(v) else None for v in vector]
        for i, vector in zip(eligible, matrix, strict=True)
    }
    predictions = []
    for i, (row, score) in enumerate(zip(rows, board, strict=True)):
        components = score["score_components"]
        raw = probability(vectors[i]) if i in vectors else None
        market = 80 * raw if raw is not None else components["market"]
        candidate = attention.attention_score(
            signal=market,
            event=components["sec_event"],
            news=components["news"],
            social=components["social_search"],
            cluster=components["cluster"],
            community=components["community"],
        )
        if raw is None:
            candidate = score["attention_score"]
        predictions.append(
            {
                "run_id": run_id,
                "ticker": row["ticker"],
                "snapshot_id": row["id"],
                "inputs_json": encode({key: row.get(key) for key in INPUT_KEYS}),
                "vector_json": encode(vectors.get(i, [])),
                "evidence_json": encode(
                    {
                        "components": components,
                        "eligibility": score["eligibility"],
                        "urgent": score["attention_urgent"],
                        "urgency_reason": score["attention_urgency_reason"],
                        "feature_as_of": row["captured_at"],
                        "evidence_as_of": at.isoformat(),
                        "tie_rank": row.get("baseline_rank"),
                    }
                ),
                "reason": reasons[i],
                "probability": raw,
                "baseline_market": components["market"],
                "candidate_market": market,
                "baseline_score": score["attention_score"],
                "candidate_score": candidate,
                "urgent": int(score["attention_urgent"]),
                "outcome_status": "pending",
            }
        )
    for score_key, rank_key, urgent in (
        ("baseline_score", "baseline_rank", True),
        ("candidate_score", "candidate_rank", True),
        ("baseline_market", "baseline_market_rank", False),
        ("candidate_market", "candidate_market_rank", False),
    ):
        order = sorted(
            range(len(rows)),
            key=lambda i: (
                -predictions[i]["urgent"] if urgent else 0,
                -predictions[i][score_key],
                attention.finite_number(rows[i].get("baseline_rank")) or 1_000_000,
                rows[i]["ticker"],
            ),
        )
        for rank, i in enumerate(order, 1):
            predictions[i][rank_key] = rank
    with connection() as db:
        for prediction in predictions:
            columns = list(prediction)
            db.execute(
                f"INSERT INTO attention_trial_predictions({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                tuple(prediction[key] for key in columns),
            )
    # The prediction bytes are durable before choosing their future price window.
    # A crash between these transactions leaves a visible 'recording' receipt.
    saved = utcnow()
    window = _window(saved)
    with connection() as db:
        if window:
            entry, end = window
            db.execute(
                """UPDATE attention_trial_runs SET status='recorded',saved_at=?,entry_at=?,
                end_at=?,deadline_at=?,expected_rows=? WHERE id=?""",
                (
                    saved.isoformat(),
                    entry.isoformat(),
                    end.isoformat(),
                    (end + timedelta(hours=6)).isoformat(),
                    len(rows),
                    run_id,
                ),
            )
            db.execute(
                "UPDATE attention_trial_predictions SET next_attempt_at=? WHERE run_id=?",
                ((end + timedelta(minutes=5)).isoformat(), run_id),
            )
        else:
            db.execute(
                """UPDATE attention_trial_runs SET status='outside_window',saved_at=?,
                expected_rows=? WHERE id=?""",
                (saved.isoformat(), len(rows), run_id),
            )
            db.execute(
                "UPDATE attention_trial_predictions SET outcome_status='outside_window' "
                "WHERE run_id=?",
                (run_id,),
            )


def archive_bars(db: Any, rows: list[tuple]) -> None:
    """Save completed observations, including unchanged final bars and revisions."""
    pending = db.execute(
        """SELECT p.ticker,r.entry_at,r.end_at,r.deadline_at FROM attention_trial_predictions p
        JOIN attention_trial_runs r ON r.id=p.run_id
        WHERE p.outcome_status='pending' AND r.status='recorded'"""
    ).fetchall()
    windows: dict[str, list[tuple]] = defaultdict(list)
    for row in pending:
        windows[row["ticker"]].append(
            tuple(
                moment(row[key])
                for key in (
                    "entry_at",
                    "end_at",
                    "deadline_at",
                )
            )
        )
    versions = []
    for source, ticker, interval, stamp, opening, high, low, close, volume, _, observed in rows:
        if source != "yahoo" or interval != "5m" or ticker not in windows:
            continue
        at, collected = moment(stamp), moment(observed)
        if at is None or collected is None or collected < at + timedelta(minutes=5):
            continue
        if not any(
            start <= at < end and collected <= deadline for start, end, deadline in windows[ticker]
        ):
            continue
        payload = {
            "source": source,
            "ticker": ticker,
            "bar_time": at.isoformat(),
            "open": opening,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
        digest = hashlib.sha256(encode(payload).encode()).hexdigest()
        versions.append((ticker, at.isoformat(), digest, collected.isoformat(), encode(payload)))
    if versions:
        db.executemany(
            """INSERT INTO attention_trial_bars(ticker,bar_time,sha256,observed_at,payload_json)
            VALUES(?,?,?,?,?) ON CONFLICT(ticker,bar_time,sha256) DO NOTHING""",
            versions,
        )


def _settle(db: Any, row: dict, at: datetime) -> bool:
    versions = db.execute(
        """SELECT * FROM attention_trial_bars WHERE ticker=? AND bar_time>=? AND bar_time<?
        AND observed_at<=? ORDER BY observed_at,sha256""",
        (row["ticker"], row["entry_at"], row["end_at"], min(at.isoformat(), row["deadline_at"])),
    ).fetchall()
    bars, hashes = {}, {}
    for version in versions:
        stamp = moment(version["bar_time"])
        if stamp not in bars:
            bars[stamp] = {
                **json.loads(version["payload_json"]),
                "last_collected_at": version["observed_at"],
            }
            hashes[version["bar_time"]] = version["sha256"]
    outcome = future_activity(moment(row["entry_at"]), bars)
    terminal = outcome["status"] == "resolved" or at >= moment(row["deadline_at"])
    if terminal:
        db.execute(
            """UPDATE attention_trial_predictions SET outcome_status=?,target=?,outcome_json=?,
            last_attempt_at=?,next_attempt_at=NULL WHERE run_id=? AND ticker=?
            AND outcome_status='pending'""",
            (
                "resolved" if outcome["status"] == "resolved" else "unknown",
                outcome["target"],
                encode({**outcome, "bar_hashes": hashes, "settled_at": at.isoformat()}),
                at.isoformat(),
                row["run_id"],
                row["ticker"],
            ),
        )
    return terminal


def refresh_outcomes(*, fetch: Any = None, at: datetime | None = None) -> dict:
    """A bounded, fair retry queue that follows stocks after they leave the scan."""
    fixed_at = at
    at = at or utcnow()
    with connection() as db:
        due = [
            dict(row)
            for row in db.execute(
                """SELECT p.*,r.entry_at,r.end_at,r.deadline_at FROM attention_trial_predictions p
            JOIN attention_trial_runs r ON r.id=p.run_id
            WHERE r.status='recorded' AND p.outcome_status='pending' AND p.next_attempt_at<=?
            ORDER BY p.next_attempt_at,p.run_id,p.ticker LIMIT 500""",
                (at.isoformat(),),
            ).fetchall()
        ]
        pending = [row for row in due if not _settle(db, row, at)]
        tickers = list(dict.fromkeys(row["ticker"] for row in pending))[:30]
        pending = [row for row in pending if row["ticker"] in tickers]
        for row in pending:
            db.execute(
                """UPDATE attention_trial_predictions SET next_attempt_at=?,attempts=attempts+1,
                last_attempt_at=? WHERE run_id=? AND ticker=? AND outcome_status='pending'""",
                (
                    (at + timedelta(minutes=5)).isoformat(),
                    at.isoformat(),
                    row["run_id"],
                    row["ticker"],
                ),
            )
    error = None
    if tickers:
        try:
            if fetch is None:
                from runner_watch.market_data import YahooMarketData
                from runner_web.ingestion import record_source_fetch

                fetch = YahooMarketData(batch_size=30, fetch_recorder=record_source_fetch).intraday
            result = fetch(tickers)
            if result.failed:
                error = "provider_missing_tickers"
            elif getattr(result, "warnings", []):
                error = "provider_recording_warning"
        except Exception as exc:
            error = type(exc).__name__
            LOG.exception("Attention outcome fetch failed")
        with connection() as db:
            for row in pending:
                db.execute(
                    "UPDATE attention_trial_predictions SET last_error=? "
                    "WHERE run_id=? AND ticker=?",
                    (error, row["run_id"], row["ticker"]),
                )
                _settle(db, row, fixed_at or utcnow())
    return {"due": len(due), "fetched_tickers": len(tickers), "error": error}


async def worker() -> None:
    await asyncio.sleep(45)
    while True:
        try:
            await asyncio.to_thread(refresh_outcomes)
        except Exception:
            LOG.exception("Attention outcome worker failed")
        await asyncio.sleep(120)


def release_report() -> dict:
    with connection() as db:
        runs = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM attention_trial_runs ORDER BY evidence_as_of"
            ).fetchall()
        ]
        records = [
            dict(r)
            for r in db.execute(
                """SELECT p.ticker,p.reason,p.outcome_status,p.target,p.baseline_rank,
                p.candidate_rank,p.baseline_market_rank,p.candidate_market_rank,
                r.day,r.scan_run_id,r.model_sha256,r.policy
            FROM attention_trial_predictions p
            JOIN attention_trial_runs r ON r.id=p.run_id WHERE r.status='recorded'
            ORDER BY r.evidence_as_of,p.ticker"""
            ).fetchall()
        ]
    report: dict[str, Any] = {
        "enabled": enabled(),
        "phase": CONTRACT["phase"],
        "model": artifact()["id"],
        "model_sha256": artifact()["sha256"],
        "contract": CONTRACT,
        "runs": dict(Counter(r["status"] for r in runs)),
        "outcomes": dict(Counter(r["outcome_status"] for r in records)),
        "fallbacks": dict(Counter(r["reason"] for r in records)),
        "last_saved_at": max((r["saved_at"] for r in runs if r["saved_at"]), default=None),
        "promotion_ready": False,
        "next_step": "Collect 10 completed sessions, then freeze a calibration artifact.",
    }
    # Compare a single exact model/policy cohort; earlier contracts remain archived.
    records = [
        r
        for r in records
        if r["model_sha256"] == artifact()["sha256"] and r["policy"] == attention.POLICY_VERSION
    ]
    pending_days = {r["day"] for r in records if r["outcome_status"] == "pending"}
    completed_days = []
    for day in sorted({r["day"] for r in records} - pending_days):
        session = _market_window(datetime.fromisoformat(f"{day}T16:00:00+00:00"))
        if session and utcnow() >= session[1] + timedelta(hours=6):
            completed_days.append(day)
    report["completed_sessions"] = len(completed_days)
    report["calibration_sessions_remaining"] = max(0, 10 - len(completed_days))
    rows = [r for r in records if r["day"] in completed_days]
    if rows:
        indices = list(range(len(rows)))
        for scope, base_key, new_key in (
            ("full_board", "baseline_rank", "candidate_rank"),
            ("market_only", "baseline_market_rank", "candidate_market_rank"),
        ):
            base = -np.array([r[base_key] for r in rows])
            new = -np.array([r[new_key] for r in rows])
            baseline, candidate = (rank_metrics(rows, v, indices) for v in (base, new))
            interval = paired_interval(candidate, baseline)
            bounds = missing_outcome_bounds(rows, new, base, indices)
            precision = baseline["daily_precision_lower"]
            lift = candidate["daily_precision_lower"] / precision - 1 if precision else None
            report[scope] = {
                "baseline": baseline,
                "candidate": candidate,
                "day_weighted_lift": lift,
                "paired_interval": interval,
                "missing_outcome_bounds": bounds,
                "coverage_gate": min(baseline["coverage"], candidate["coverage"]) >= 0.90,
                "lift_gate": lift is not None and lift >= 0.10,
                "confidence_gate": interval["ci95"][0] > 0,
                "missing_outcome_gate": bounds["mean_daily_difference_bounds"][0] > 0,
            }
    return report


if __name__ == "__main__":
    print(json.dumps(release_report(), indent=2, allow_nan=False))
