from __future__ import annotations

import logging
import math
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from runner_web.cases import update_case
from runner_web.db import connection
from runner_web.labels import (  # noqa: F401  (re-exported for callers)
    AMBIGUOUS,
    BAR_TOLERANCE,
    BARRIER_HORIZON,
    LOWER_BARRIER_PCT,
    RESOLVED,
    UPPER_BARRIER_PCT,
)
from runner_web.labels import POLICY_VERSION as BARRIER_POLICY_VERSION  # noqa: F401
from runner_web.outcome_bars import Bar, IndexedBars
from runner_web.ranker import sync_training_outcome

LOG = logging.getLogger(__name__)
HORIZONS = {"1h": timedelta(hours=1), "1d": timedelta(days=1), "5d": timedelta(days=5)}
EASTERN = ZoneInfo("America/New_York")
OUTCOME_REFRESH_TICKER_LIMIT = max(50, int(os.getenv("OUTCOME_REFRESH_TICKER_LIMIT", "200")))
CASE_OUTCOME_GRACE = timedelta(days=4)


def iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def return_pct(base_price: float, later_price: float) -> float | None:
    if not all(math.isfinite(value) and value > 0 for value in (base_price, later_price)):
        return None
    return round((later_price / base_price - 1) * 100, 3)


def due_horizons(row: dict[str, Any], at: datetime | None = None) -> list[str]:
    current = at or datetime.now(UTC)
    base_at = datetime.fromisoformat(str(row["base_at"]))
    if base_at.tzinfo is None:
        base_at = base_at.replace(tzinfo=UTC)
    age = current - base_at.astimezone(UTC)
    return [
        label
        for label, wait in HORIZONS.items()
        if age >= wait and row.get(f"return_{label}_pct") is None
    ]


def _parsed_moment(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _earliest_observation(moments: list[datetime | None]) -> datetime | None:
    known = [moment for moment in moments if moment is not None]
    return min(known) - timedelta(days=1) if known else None


def _state(key: str, value: str, timestamp: str) -> None:
    with connection() as db:
        db.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, value, timestamp),
        )


def refresh_outcomes(at: datetime | None = None) -> dict[str, Any]:

    current = at or datetime.now(UTC)
    timestamp = iso(current)
    cutoff = iso(current - timedelta(days=7))
    with connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO sec_outcomes(accession,base_price,base_at,updated_at)
            SELECT accession,price,created_at,? FROM sec_filings
            WHERE price IS NOT NULL AND price>0 AND created_at>=?
            """,
            (timestamp, cutoff),
        )
        rows = db.execute(
            """
            SELECT o.*,f.ticker FROM sec_outcomes o
            JOIN sec_filings f ON f.accession=o.accession
            WHERE o.base_at>=?
            """,
            (cutoff,),
        ).fetchall()

    pending: list[tuple[dict[str, Any], list[str]]] = []
    for raw in rows:
        row = dict(raw)
        horizons = due_horizons(row, current)
        if horizons:
            pending.append((row, horizons))
    # Rows that keep failing carry a later next_attempt_at, so the oldest *due*
    # observation is served first and nothing waits behind a stuck one.
    pending.sort(
        key=lambda item: (
            str(item[0].get("next_attempt_at") or item[0].get("base_at") or ""),
            str(item[0].get("base_at") or ""),
        )
    )
    pending = pending[:OUTCOME_REFRESH_TICKER_LIMIT]
    tickers = [str(row["ticker"]) for row, _ in pending]
    since = _earliest_observation([_parsed_moment(row.get("base_at")) for row, _ in pending])
    prices = _bar_prices(tickers, since=since)

    samples_added = 0
    with connection() as db:
        for row, horizons in pending:
            changes: dict[str, Any] = {"updated_at": timestamp}
            try:
                base_at = datetime.fromisoformat(str(row["base_at"]))
            except ValueError:
                continue
            if base_at.tzinfo is None:
                base_at = base_at.replace(tzinfo=UTC)
            ticker_bars = prices.get(str(row["ticker"]), [])
            for horizon in horizons:
                observed = _scan_horizon_price(ticker_bars, base_at, horizon)
                if observed is None:
                    continue
                price, observed_at = observed
                result = return_pct(float(row["base_price"]), price)
                if result is None:
                    continue
                changes[f"price_{horizon}"] = price
                changes[f"return_{horizon}_pct"] = result
                changes[f"observed_{horizon}_at"] = iso(observed_at)
                samples_added += 1
            if len(changes) == 1:
                continue
            assignments = ",".join(f"{column}=?" for column in changes)
            db.execute(
                f"UPDATE sec_outcomes SET {assignments} WHERE accession=?",
                (*changes.values(), row["accession"]),
            )
        labeled = int(
            db.execute(
                """
                SELECT COUNT(*) FROM sec_outcomes
                WHERE return_1h_pct IS NOT NULL OR return_1d_pct IS NOT NULL
                      OR return_5d_pct IS NOT NULL
                """
            ).fetchone()[0]
        )

    _state("outcomes_last_refresh", timestamp, timestamp)
    _state("outcomes_labeled_events", str(labeled), timestamp)
    _state("outcomes_last_samples_added", str(samples_added), timestamp)
    return {"events": len(rows), "labeled_events": labeled, "samples_added": samples_added}


def record_outcome_error(exc: Exception) -> None:
    LOG.exception("Outcome sampling failed")
    timestamp = iso()
    _state("outcomes_last_error", str(exc)[:500], timestamp)


RETRY_BASE_SECONDS = 300
RETRY_MAX_SECONDS = 6 * 3600


def _retry_delay(attempts: int) -> timedelta:
    """Exponential backoff, capped, so a stuck row keeps its place in line."""

    seconds = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** max(0, attempts - 1)))
    return timedelta(seconds=seconds)


def outcome_coverage(at: datetime | None = None) -> dict[str, Any]:
    """How much of the outcome window can actually be accounted for."""

    current = at or datetime.now(UTC)
    with connection() as db:
        row = db.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN barrier_label IS NOT NULL THEN 1 ELSE 0 END)
                       AS labeled,
                   SUM(CASE WHEN barrier_resolution='ambiguous' THEN 1 ELSE 0 END)
                       AS ambiguous,
                   SUM(CASE WHEN barrier_label IS NULL AND attempts>0 THEN 1 ELSE 0 END)
                       AS retrying,
                   SUM(CASE WHEN barrier_label IS NULL AND attempts>=5 THEN 1 ELSE 0 END)
                       AS stuck
            FROM scan_outcomes
            WHERE base_at>=?
            """,
            (iso(current - timedelta(days=10)),),
        ).fetchone()
    values = dict(row) if row else {}
    total = int(values.get("total") or 0)
    labeled = int(values.get("labeled") or 0)
    return {
        "total": total,
        "labeled": labeled,
        "ambiguous": int(values.get("ambiguous") or 0),
        "retrying": int(values.get("retrying") or 0),
        "stuck": int(values.get("stuck") or 0),
        "labeled_pct": round(labeled / total * 100, 2) if total else None,
    }


def _bar_prices(tickers: list[str], *, since: datetime | None = None) -> dict[str, IndexedBars]:
    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    placeholders = ",".join("?" for _ in unique)
    window = ""
    parameters: list[Any] = [*unique]
    if since is not None:
        window = " AND bar_time>=?"
        parameters.append(since.astimezone(UTC).isoformat())
    output: dict[str, list[Bar]] = {}
    with connection() as db:
        rows = db.execute(
            f"""
            SELECT ticker,bar_time,high,low,close FROM market_bars
            WHERE source='yahoo' AND interval='5m' AND close>0
                  AND ticker IN ({placeholders}){window}
            ORDER BY ticker,bar_time
            """,
            parameters,
        )
        for row in rows:
            try:
                stamp = datetime.fromisoformat(str(row["bar_time"]))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=UTC)
                close = float(row["close"])
                high = float(row["high"]) if row["high"] is not None else close
                low = float(row["low"]) if row["low"] is not None else close
                output.setdefault(str(row["ticker"]), []).append(
                    (stamp.astimezone(UTC), high, low, close)
                )
            except (TypeError, ValueError):
                continue
    return {ticker: IndexedBars(bars) for ticker, bars in output.items()}


def _first_price_at_or_after(
    bars: Sequence[Bar], target: datetime
) -> tuple[float, datetime] | None:
    if isinstance(bars, IndexedBars):
        bar = bars.first_at_or_after(target)
        return (bar[3], bar[0]) if bar is not None else None
    target_utc = target.astimezone(UTC)
    return next(((close, stamp) for stamp, _, _, close in bars if stamp >= target_utc), None)


def _price_near_target(bars: Sequence[Bar], target: datetime) -> tuple[float, datetime] | None:

    if isinstance(bars, IndexedBars):
        return bars.near(target, BAR_TOLERANCE)
    target_utc = target.astimezone(UTC)
    after = next(
        (
            (close, stamp)
            for stamp, _, _, close in bars
            if target_utc <= stamp <= target_utc + BAR_TOLERANCE
        ),
        None,
    )
    if after is not None:
        return after
    before = [
        (close, stamp)
        for stamp, _, _, close in bars
        if target_utc - BAR_TOLERANCE <= stamp < target_utc
    ]
    return before[-1] if before else None


def _scan_horizon_price(
    bars: Sequence[Bar], base_at: datetime, horizon: str
) -> tuple[float, datetime] | None:
    if horizon == "1h":
        return _price_near_target(bars, base_at + timedelta(hours=1))
    base_date = base_at.astimezone(EASTERN).date()
    if isinstance(bars, IndexedBars):
        return bars.session_close(base_date, 0 if horizon == "1d" else 4)
    by_date: dict[Any, list[tuple[datetime, float]]] = {}
    for stamp, _, _, close in bars:
        session_date = stamp.astimezone(EASTERN).date()
        if session_date > base_date:
            by_date.setdefault(session_date, []).append((stamp, close))
    session_dates = sorted(by_date)
    offset = 0 if horizon == "1d" else 4
    if len(session_dates) <= offset:
        return None
    stamp, price = by_date[session_dates[offset]][-1]
    return price, stamp


def barrier_outcome(
    bars: Sequence[Bar], base_at: datetime, base_price: float
) -> dict[str, Any] | None:

    base_utc = base_at.astimezone(UTC)
    target = base_utc + BARRIER_HORIZON
    window = (
        bars.between(base_utc, target)
        if isinstance(bars, IndexedBars)
        else [bar for bar in bars if base_utc < bar[0] <= target]
    )
    if not window or not math.isfinite(base_price) or base_price <= 0:
        return None

    upper = base_price * (1 + UPPER_BARRIER_PCT / 100)
    lower = base_price * (1 - LOWER_BARRIER_PCT / 100)
    window.sort(key=lambda bar: bar[0])
    maximum = max(bar[1] for bar in window)
    minimum = min(bar[2] for bar in window)
    maximum_return = return_pct(base_price, maximum)
    minimum_return = return_pct(base_price, minimum)
    result: dict[str, Any] = {
        "upper_barrier_pct": UPPER_BARRIER_PCT,
        "lower_barrier_pct": LOWER_BARRIER_PCT,
        "horizon_minutes": int(BARRIER_HORIZON.total_seconds() / 60),
        "max_favorable_pct": max(0.0, maximum_return or 0.0),
        "max_adverse_pct": min(0.0, minimum_return or 0.0),
    }
    previous = base_utc
    for stamp, high, low, _ in window:
        if stamp - previous > BAR_TOLERANCE:
            return None
        previous = stamp
        touched_up = high >= upper
        touched_down = low <= lower
        if touched_up and touched_down:
            # One bar cannot say which barrier came first. The pessimistic view
            # still labels it down, but it is marked ambiguous so training can
            # hold it out instead of learning a coin flip as a fact.
            result.update(
                barrier_label="down",
                barrier_hit_at=iso(stamp),
                barrier_ambiguous=1,
                barrier_resolution=AMBIGUOUS,
            )
            return result
        if touched_down:
            result.update(
                barrier_label="down",
                barrier_hit_at=iso(stamp),
                barrier_ambiguous=0,
                barrier_resolution=RESOLVED,
            )
            return result
        if touched_up:
            result.update(
                barrier_label="up",
                barrier_hit_at=iso(stamp),
                barrier_ambiguous=0,
                barrier_resolution=RESOLVED,
            )
            return result

    if target - previous <= BAR_TOLERANCE:
        result.update(
            barrier_label="timeout",
            barrier_hit_at=None,
            barrier_ambiguous=0,
            barrier_resolution=RESOLVED,
        )
        return result
    return None


def case_horizon_outcome(
    bars: Sequence[Bar],
    base_at: datetime,
    base_price: float,
    horizon_minutes: int,
    *,
    at: datetime | None = None,
) -> dict[str, Any] | None:

    current = (at or datetime.now(UTC)).astimezone(UTC)
    base_utc = base_at.astimezone(UTC)
    due = base_utc + timedelta(minutes=horizon_minutes)
    if current < due:
        return None
    if isinstance(bars, IndexedBars):
        observed = bars.first_at_or_after(due)
        if observed is not None and observed[0] > min(current, due + CASE_OUTCOME_GRACE):
            observed = None
    else:
        observed = next(
            (
                (stamp, high, low, close)
                for stamp, high, low, close in bars
                if due <= stamp <= min(current, due + CASE_OUTCOME_GRACE)
            ),
            None,
        )
    if observed is None:
        return None
    observed_at, _, _, end_price = observed
    window = (
        bars.between(base_utc, observed_at)
        if isinstance(bars, IndexedBars)
        else [bar for bar in bars if base_utc < bar[0] <= observed_at]
    )
    if not window:
        return None
    result = return_pct(base_price, end_price)
    if result is None:
        return None
    max_high = max(float(bar[1]) for bar in window)
    min_low = min(float(bar[2]) for bar in window)
    direction = "up" if result > 0.5 else "down" if result < -0.5 else "flat"
    return {
        "end_price": float(end_price),
        "observed_at": iso(observed_at),
        "return_pct": result,
        "return_direction": direction,
        "max_favorable_pct": return_pct(base_price, max_high),
        "max_adverse_pct": return_pct(base_price, min_low),
    }


def _horizon_label(minutes: int) -> str:
    if minutes % (30 * 1440) == 0:
        return f"{minutes // (30 * 1440)}mo"
    if minutes % (7 * 1440) == 0:
        return f"{minutes // (7 * 1440)}w"
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def refresh_case_outcomes(at: datetime | None = None) -> dict[str, Any]:

    current = (at or datetime.now(UTC)).astimezone(UTC)
    timestamp = iso(current)
    with connection() as db:
        cases = [
            dict(row)
            for row in db.execute(
                """
                SELECT c.* FROM thesis_cases c
                LEFT JOIN thesis_case_outcomes o ON o.case_id=c.id
                WHERE c.status='active' AND c.reference_price>0
                      AND (o.status IS NULL OR o.status='pending')
                """
            ).fetchall()
        ]
        for case in cases:
            try:
                base_at = datetime.fromisoformat(str(case["reference_at"]))
            except ValueError:
                continue
            if base_at.tzinfo is None:
                base_at = base_at.replace(tzinfo=UTC)
            due_at = base_at.astimezone(UTC) + timedelta(minutes=int(case["horizon_minutes"]))
            db.execute(
                """
                INSERT INTO thesis_case_outcomes(
                    case_id,ticker,base_price,base_at,horizon_minutes,due_at,
                    status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'pending',?,?)
                ON CONFLICT(case_id) DO UPDATE SET
                    horizon_minutes=excluded.horizon_minutes,
                    due_at=excluded.due_at,updated_at=excluded.updated_at
                WHERE thesis_case_outcomes.status='pending'
                """,
                (
                    case["id"],
                    case["ticker"],
                    case["reference_price"],
                    iso(base_at.astimezone(UTC)),
                    case["horizon_minutes"],
                    iso(due_at),
                    timestamp,
                    timestamp,
                ),
            )

    due_cases: list[dict[str, Any]] = []
    for case in cases:
        try:
            base_at = datetime.fromisoformat(str(case["reference_at"]))
        except ValueError:
            continue
        if base_at.tzinfo is None:
            base_at = base_at.replace(tzinfo=UTC)
        if current >= base_at.astimezone(UTC) + timedelta(minutes=int(case["horizon_minutes"])):
            due_cases.append(case)
    due_cases.sort(key=lambda case: str(case.get("reference_at") or ""))
    due_cases = due_cases[:OUTCOME_REFRESH_TICKER_LIMIT]
    tickers = [str(case["ticker"]) for case in due_cases]
    since = _earliest_observation([_parsed_moment(case.get("reference_at")) for case in due_cases])
    archived = _bar_prices(tickers, since=since)
    completed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    with connection() as db:
        for case in due_cases:
            try:
                base_at = datetime.fromisoformat(str(case["reference_at"]))
            except ValueError:
                continue
            if base_at.tzinfo is None:
                base_at = base_at.replace(tzinfo=UTC)
            outcome = case_horizon_outcome(
                archived.get(str(case["ticker"]), []),
                base_at,
                float(case["reference_price"]),
                int(case["horizon_minutes"]),
                at=current,
            )
            if outcome is None:
                continue
            db.execute(
                """
                UPDATE thesis_case_outcomes SET
                    status='complete',end_price=?,observed_at=?,return_pct=?,
                    return_direction=?,max_favorable_pct=?,max_adverse_pct=?,updated_at=?
                WHERE case_id=? AND status='pending'
                """,
                (
                    outcome["end_price"],
                    outcome["observed_at"],
                    outcome["return_pct"],
                    outcome["return_direction"],
                    outcome["max_favorable_pct"],
                    outcome["max_adverse_pct"],
                    timestamp,
                    case["id"],
                ),
            )
            completed.append((case, outcome))

    for case, outcome in completed:
        horizon = _horizon_label(int(case["horizon_minutes"]))
        summary = (
            f"{horizon} view ended {float(outcome['return_pct']):+.1f}% "
            f"at ${float(outcome['end_price']):.4g}."
        )
        closed = update_case(
            str(case["user_id"]),
            str(case["public_id"]),
            {"status": "closed", "final_outcome": summary},
            change_note="Closed automatically at the comment's inferred horizon",
        )
        if not closed:
            continue
        with connection() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO thesis_case_updates(
                    id,case_id,kind,direction,summary,recommended_action,
                    confidence_before,confidence_after,citations_json,
                    evidence_fingerprint,deterministic_veto_json,created_at
                ) VALUES(?,?,?,'unchanged',?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()),
                    case["id"],
                    "outcome",
                    summary,
                    "The view reached its horizon. A later comment starts a new view.",
                    case.get("confidence"),
                    case.get("confidence"),
                    "[]",
                    f"case-outcome:{case['id']}:{outcome['observed_at']}",
                    "{}",
                    outcome["observed_at"],
                ),
            )

    _state("case_outcomes_last_refresh", timestamp, timestamp)
    _state("case_outcomes_completed", str(len(completed)), timestamp)
    return {"pending": len(cases), "due": len(due_cases), "completed": len(completed)}


# Tuple order is shared by selection and the collector below.
PendingScan = tuple[dict[str, Any], list[str], bool, bool]


def _pending_scan_outcomes(
    database: Any, current: datetime, cutoff: str, *, limit: int
) -> tuple[list[PendingScan], int, int]:
    """Read only enough keyset pages to fill the existing oldest-due-first budget.

    Maturity remains a Python datetime comparison, including offset timestamps.
    Pushing it into a lexical TEXT cutoff would subtly change legacy outcomes.
    The count retains the old `rows` telemetry without materializing those rows.
    """
    if limit < 1:
        raise ValueError("Outcome batch limit must be positive")
    timestamp = iso(current)
    window = "base_at>=? AND (next_attempt_at IS NULL OR next_attempt_at<=?)"
    total = int(
        database.execute(
            f"SELECT COUNT(*) FROM scan_outcomes WHERE {window}", (cutoff, timestamp)
        ).fetchone()[0]
    )
    pending: list[PendingScan] = []
    examined = 0
    after: tuple[str, str] | None = None
    while len(pending) < limit:
        parameters: list[Any] = [cutoff, timestamp]
        cursor_clause = ""
        if after is not None:
            cursor_clause = " AND (base_at,snapshot_id)>(?,?)"
            parameters.extend(after)
        parameters.append(limit)
        rows = database.execute(
            f"""
            SELECT * FROM scan_outcomes WHERE {window}
              AND (barrier_label IS NULL OR return_60m_pct IS NULL
                   OR return_1h_pct IS NULL OR return_1d_pct IS NULL OR return_5d_pct IS NULL)
              {cursor_clause}
            ORDER BY base_at,snapshot_id LIMIT ?
            """,
            parameters,
        ).fetchall()
        if not rows:
            break
        examined += len(rows)
        for raw in rows:
            row = dict(raw)
            horizons = due_horizons(row, current)
            base_at = _parsed_moment(row["base_at"])
            barrier_mature = (
                base_at is not None and current.astimezone(UTC) - base_at >= BARRIER_HORIZON
            )
            barrier_due = barrier_mature and row.get("barrier_label") is None
            terminal_due = barrier_mature and row.get("return_60m_pct") is None
            if horizons or barrier_due or terminal_due:
                pending.append((row, horizons, barrier_due, terminal_due))
                if len(pending) == limit:
                    break
        if len(rows) < limit:
            break
        after = (str(rows[-1]["base_at"]), str(rows[-1]["snapshot_id"]))
    return pending, total, examined


def refresh_scan_outcomes(at: datetime | None = None) -> dict[str, Any]:

    current = at or datetime.now(UTC)
    timestamp = iso(current)
    cutoff = iso(current - timedelta(days=10))
    with connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO scan_outcomes(
                snapshot_id,ticker,base_price,base_at,upper_barrier_pct,
                lower_barrier_pct,horizon_minutes,updated_at
            )
            SELECT id,ticker,price,captured_at,?,?,?,? FROM scan_snapshots
            WHERE scan_run_id IS NOT NULL AND price>0 AND captured_at>=?
            """,
            (UPPER_BARRIER_PCT, LOWER_BARRIER_PCT, 60, timestamp, cutoff),
        )
        db.execute(
            """
            UPDATE scan_outcomes SET base_at=(
                SELECT captured_at FROM scan_snapshots
                WHERE scan_snapshots.id=scan_outcomes.snapshot_id
            )
            WHERE barrier_label IS NULL AND return_1h_pct IS NULL
                  AND return_1d_pct IS NULL AND return_5d_pct IS NULL
                  AND EXISTS (
                      SELECT 1 FROM scan_snapshots s
                      WHERE s.id=scan_outcomes.snapshot_id
                        AND s.captured_at<>scan_outcomes.base_at
                  )
            """
        )
        pending, row_count, examined = _pending_scan_outcomes(
            db, current, cutoff, limit=OUTCOME_REFRESH_TICKER_LIMIT
        )

    tickers = [str(row["ticker"]) for row, _, _, _ in pending]
    since = _earliest_observation([_parsed_moment(row.get("base_at")) for row, _, _, _ in pending])
    prices = _bar_prices(tickers, since=since)

    samples_added = 0
    barrier_labels_added = 0
    deferred = 0
    with connection() as db:
        for row, horizons, barrier_due, terminal_due in pending:
            try:
                base_at = datetime.fromisoformat(str(row["base_at"]))
            except ValueError:
                continue
            if base_at.tzinfo is None:
                base_at = base_at.replace(tzinfo=UTC)
            changes: dict[str, Any] = {"updated_at": timestamp}
            ticker_bars = prices.get(str(row["ticker"]), [])
            if barrier_due:
                barrier = barrier_outcome(ticker_bars, base_at, float(row["base_price"]))
                if barrier is not None:
                    changes.update(barrier)
                    barrier_labels_added += 1
            if terminal_due:
                observed_60m = _price_near_target(ticker_bars, base_at + BARRIER_HORIZON)
                if observed_60m is not None:
                    price_60m, observed_60m_at = observed_60m
                    changes["price_60m"] = price_60m
                    changes["return_60m_pct"] = return_pct(float(row["base_price"]), price_60m)
                    changes["observed_60m_at"] = iso(observed_60m_at)
            for horizon in horizons:
                observed = _scan_horizon_price(ticker_bars, base_at, horizon)
                if observed is None:
                    continue
                price, observed_at = observed
                result = return_pct(float(row["base_price"]), price)
                if result is None:
                    continue
                changes[f"price_{horizon}"] = price
                changes[f"return_{horizon}_pct"] = result
                changes[f"observed_{horizon}_at"] = iso(observed_at)
                samples_added += 1
            if len(changes) == 1:
                # Nothing could be read yet: back this row off so a newer
                # observation gets the slot next cycle.
                attempts = int(row.get("attempts") or 0) + 1
                changes["attempts"] = attempts
                changes["last_attempt_at"] = timestamp
                changes["next_attempt_at"] = iso(current + _retry_delay(attempts))
                deferred += 1
            else:
                # Progress was made; the next cycle may look again immediately.
                changes["attempts"] = 0
                changes["next_attempt_at"] = None
                changes["last_attempt_at"] = timestamp
            assignments = ",".join(f"{column}=?" for column in changes)
            db.execute(
                f"UPDATE scan_outcomes SET {assignments} WHERE snapshot_id=?",
                (*changes.values(), row["snapshot_id"]),
            )
            effective = {**row, **changes}
            if effective.get("barrier_label") and (
                "barrier_label" in changes or "return_60m_pct" in changes
            ):
                sync_training_outcome(
                    db,
                    str(row["snapshot_id"]),
                    str(effective["barrier_label"]),
                    effective.get("return_60m_pct"),
                    timestamp,
                    barrier_resolution=str(effective.get("barrier_resolution") or RESOLVED),
                )
        labeled = int(
            db.execute(
                """
                SELECT COUNT(*) FROM scan_outcomes
                WHERE return_1h_pct IS NOT NULL OR return_1d_pct IS NOT NULL
                      OR return_5d_pct IS NOT NULL
                """
            ).fetchone()[0]
        )
        barrier_labeled = int(
            db.execute(
                "SELECT COUNT(*) FROM scan_outcomes WHERE barrier_label IS NOT NULL"
            ).fetchone()[0]
        )

    _state("scan_outcomes_last_refresh", timestamp, timestamp)
    _state("scan_outcomes_deferred_rows", str(deferred), timestamp)
    _state("scan_outcomes_labeled_rows", str(labeled), timestamp)
    _state("scan_outcomes_barrier_labeled_rows", str(barrier_labeled), timestamp)
    _state("scan_outcomes_last_samples_added", str(samples_added), timestamp)
    return {
        "rows": row_count,
        "candidate_rows_examined": examined,
        "labeled_rows": labeled,
        "barrier_labeled_rows": barrier_labeled,
        "barrier_labels_added": barrier_labels_added,
        "samples_added": samples_added,
        "deferred": deferred,
    }
