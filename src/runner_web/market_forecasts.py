from __future__ import annotations

import json
import math
import secrets
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from zoneinfo import ZoneInfo

from runner_web.ai_kol import FLASH, actor_snapshot
from runner_web.collection import recording_market_data
from runner_web.db import connection

EASTERN = ZoneInfo("America/New_York")
CONTRACT_VERSION = "premarket-eod-target-v1"
MAX_ATTEMPTS = 3
PRICE_MAX_AGE = timedelta(minutes=30)
DATA_GRACE = timedelta(days=7)
TargetGenerator = Callable[[dict[str, Any]], dict[str, Any]]


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def _stamp(value: Any) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (ValueError, TypeError):
        return None


def _at(day: str, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(
        date.fromisoformat(day), clock_time(hour, minute), tzinfo=EASTERN
    ).astimezone(UTC)


def _price(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (ValueError, TypeError):
        return None
    return round(price, 4) if math.isfinite(price) and round(price, 4) > 0 else None


def queue_market_forecasts(
    database: Any, report_id: str, report_day: str, leaders: list[dict[str, Any]], at: datetime
) -> None:
    """Queue targets with the same frozen evidence as the new briefing."""

    current = _utc(at)
    if current >= _at(report_day, 9, 30):
        return
    inputs = []
    for leader in leaders:
        row = dict(leader)
        price = _price(row.get("price"))
        quote_at = _stamp(row.get("quote_time"))
        pass_reason = None
        if (
            price is None
            or quote_at is None
            or not timedelta(0) <= current - quote_at <= PRICE_MAX_AGE
        ):
            pass_reason = "A fresh pre-market price is needed."
        elif str(row.get("trade_state")) in {"AVOID", "EXIT"}:
            pass_reason = "The saved risk state calls for a pass."
        row.update(price=price, pass_reason=pass_reason)
        inputs.append(row)
    request = {
        "actor": actor_snapshot(FLASH),
        "contract_version": CONTRACT_VERSION,
        "report_day": report_day,
        "evidence_as_of": current.isoformat(),
        "horizon": "the regular-session closing price on report_day",
        "scoring": "up: close >= target; down: close <= target; equality counts as a hit",
        "leaders": inputs,
    }
    database.execute(
        """
        INSERT INTO market_report_forecast_jobs(
            report_id,report_day,status,request_json,created_at,updated_at
        ) VALUES(?,?,'queued',?,?,?) ON CONFLICT(report_id) DO NOTHING
        """,
        (report_id, report_day, json.dumps(request), current.isoformat(), current.isoformat()),
    )


def _validated_targets(request: dict[str, Any], result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict) or result.get("model") != request["actor"]["model"]:
        raise ValueError("The response must use the saved Flash model.")
    values = result.get("forecasts")
    if not isinstance(values, list):
        raise ValueError("The response must contain a forecast list.")
    eligible = {row["ticker"]: row for row in request["leaders"] if not row["pass_reason"]}
    forecasts: dict[str, dict[str, Any]] = {}
    for raw in values:
        if not isinstance(raw, dict):
            raise ValueError("Each forecast must be an object.")
        ticker = raw.get("ticker")
        if not isinstance(ticker, str) or ticker not in eligible or ticker in forecasts:
            raise ValueError("Each requested ticker must appear exactly once.")
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Each forecast must include a reason.")
        if "target_price" not in raw:
            raise ValueError("Each forecast must include a target price or an explicit pass.")
        target = _price(raw["target_price"])
        if raw["target_price"] is not None and target is None:
            raise ValueError("A target must be a finite positive price.")
        reference = eligible[ticker]["price"]
        if target == reference:
            raise ValueError("A directional target must differ from the reference price.")
        forecasts[ticker] = {
            "target_price": target,
            "direction": "pass" if target is None else "up" if target > reference else "down",
            "reason": " ".join(reason.split())[:500],
        }
    if forecasts.keys() != eligible.keys():
        raise ValueError("Every requested ticker needs a forecast.")
    return [
        {
            "ticker": row["ticker"],
            "reference_price": row["price"],
            "reference_at": row.get("quote_time"),
            **(
                forecasts[row["ticker"]]
                if not row["pass_reason"]
                else {
                    "target_price": None,
                    "direction": "pass",
                    "reason": row["pass_reason"],
                }
            ),
        }
        for row in request["leaders"]
    ]


def generate_market_forecasts(
    generate: TargetGenerator | None, at: datetime | None = None
) -> dict[str, int]:
    started = time.monotonic()
    current = _utc(at)
    today = current.astimezone(EASTERN).date().isoformat()
    timestamp = current.isoformat()
    cutoff = _at(today, 9, 30)
    with connection() as database:
        expired = database.execute(
            """
            UPDATE market_report_forecast_jobs SET status='expired',updated_at=?
            WHERE status IN ('queued','running') AND (report_day<? OR (report_day=? AND ?=1))
            """,
            (timestamp, today, today, int(current >= cutoff)),
        ).rowcount
        if current >= cutoff or current.weekday() >= 5:
            return {"completed": 0, "expired": expired, "failed": 0}
        job = database.execute(
            """
            SELECT * FROM market_report_forecast_jobs WHERE report_day=? AND attempts<?
              AND (status='queued' OR (status='running' AND lease_until<=?))
            ORDER BY created_at LIMIT 1
            """,
            (today, MAX_ATTEMPTS, timestamp),
        ).fetchone()
        if not job:
            return {"completed": 0, "expired": expired, "failed": 0}
        request = json.loads(job["request_json"])
        needs_model = any(not row["pass_reason"] for row in request["leaders"])
        if needs_model and generate is None:
            return {"completed": 0, "expired": expired, "failed": 0}
        token = secrets.token_urlsafe(16)
        claimed = database.execute(
            """
            UPDATE market_report_forecast_jobs
            SET status='running',attempts=attempts+1,lease_token=?,lease_until=?,updated_at=?
            WHERE report_id=? AND attempts<?
              AND (status='queued' OR (status='running' AND lease_until<=?))
            """,
            (
                token,
                (current + timedelta(minutes=3)).isoformat(),
                timestamp,
                job["report_id"],
                MAX_ATTEMPTS,
                timestamp,
            ),
        ).rowcount
    if not claimed:
        return {"completed": 0, "expired": expired, "failed": 0}

    try:
        result = (
            generate(request)
            if needs_model and generate
            else {
                "model": request["actor"]["model"],
                "forecasts": [],
            }
        )
        targets = _validated_targets(request, result)
    except Exception as exc:
        with connection() as database:
            database.execute(
                """
                UPDATE market_report_forecast_jobs
                SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
                    last_error=?,updated_at=?
                WHERE report_id=? AND status='running' AND lease_token=?
                """,
                (MAX_ATTEMPTS, type(exc).__name__, timestamp, job["report_id"], token),
            )
        return {"completed": 0, "expired": expired, "failed": 1}

    # Account for the provider call before publishing a pre-open forecast.
    finished = current + timedelta(seconds=max(0, time.monotonic() - started))
    status = "complete" if finished < cutoff else "expired"
    with connection() as database:
        updated = database.execute(
            """
            UPDATE market_report_forecast_jobs
            SET status=?,response_id=?,last_error=NULL,updated_at=?
            WHERE report_id=? AND status='running' AND lease_token=?
            """,
            (
                status,
                str(result.get("request_id") or "")[:160],
                finished.isoformat(),
                job["report_id"],
                token,
            ),
        ).rowcount
        if updated and status == "complete":
            for target in targets:
                database.execute(
                    """
                    INSERT INTO market_report_forecasts(
                        report_id,ticker,report_day,reference_price,reference_at,target_price,
                        direction,reason,model,contract_version,forecast_at,status
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        job["report_id"],
                        target["ticker"],
                        today,
                        target["reference_price"],
                        target["reference_at"],
                        target["target_price"],
                        target["direction"],
                        target["reason"],
                        request["actor"]["model"],
                        request["contract_version"],
                        finished.isoformat(),
                        "pass" if target["direction"] == "pass" else "pending",
                    ),
                )
    return {
        "completed": int(bool(updated) and status == "complete"),
        "expired": expired + int(bool(updated) and status == "expired"),
        "failed": 0,
    }


def _closing_bar(
    database: Any, forecast: dict[str, Any], current: datetime
) -> dict[str, Any] | None:
    day = forecast["report_day"]
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    rows = database.execute(
        """
        SELECT source,interval,bar_time,close,last_collected_at FROM market_bars
        WHERE ticker=? AND interval IN ('1d','5m') AND bar_time>=? AND bar_time<?
          AND source IN ('massive','yahoo')
        ORDER BY CASE interval WHEN '1d' THEN 0 ELSE 1 END,
                 CASE source WHEN 'massive' THEN 0 ELSE 1 END,bar_time DESC
        """,
        (forecast["ticker"], day, next_day),
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        bar_at = _stamp(row["bar_time"])
        collected = _stamp(row["last_collected_at"])
        close = _price(row["close"])
        if bar_at is None or collected is None or close is None:
            continue
        if not _at(day, 16, 15) <= collected <= current:
            continue
        if row["interval"] == "5m" and bar_at != _at(day, 15, 55):
            continue
        return {**row, "close": close}
    return None


def settle_market_forecasts(
    at: datetime | None = None, *, fetch_market_data: bool = True
) -> dict[str, int]:
    started = time.monotonic()
    current = _utc(at)
    with connection() as database:
        pending = [
            dict(row)
            for row in database.execute(
                "SELECT * FROM market_report_forecasts WHERE status='pending' ORDER BY report_day"
            ).fetchall()
            if current >= _at(row["report_day"], 16, 15)
        ]
    tickers = sorted(
        {row["ticker"] for row in pending if current - _at(row["report_day"], 16, 15) <= DATA_GRACE}
    )
    fetch_failed = 0
    if tickers and fetch_market_data:
        try:
            with recording_market_data(batch_size=60) as market_data:
                market_data.daily(tickers)
        except Exception:
            fetch_failed = 1
    current += timedelta(seconds=max(0, time.monotonic() - started))
    timestamp = current.isoformat()
    resolved = reviewed = 0
    with connection() as database:
        for forecast in pending:
            closing = _closing_bar(database, forecast, current)
            review_reason = None
            if closing:
                action = database.execute(
                    """
                    SELECT 1 FROM public_market_events WHERE ticker=? AND event_at>? AND event_at<=?
                      AND event_type IN ('reverse_split','corporate_action','security_action')
                    LIMIT 1
                    """,
                    (forecast["ticker"], forecast["reference_at"], closing["last_collected_at"]),
                ).fetchone()
                if action:
                    review_reason = "A corporate action needs a price review."
            elif current - _at(forecast["report_day"], 16, 15) > DATA_GRACE:
                review_reason = "The session closing price needs review."
            if review_reason:
                reviewed += database.execute(
                    """
                    UPDATE market_report_forecasts SET status='review',review_reason=?,settled_at=?
                    WHERE report_id=? AND ticker=? AND status='pending'
                    """,
                    (review_reason, timestamp, forecast["report_id"], forecast["ticker"]),
                ).rowcount
            elif closing:
                hit = (
                    closing["close"] >= forecast["target_price"]
                    if forecast["direction"] == "up"
                    else closing["close"] <= forecast["target_price"]
                )
                resolved += database.execute(
                    """
                    UPDATE market_report_forecasts SET status=?,close_price=?,close_bar_at=?,
                        close_source=?,close_interval=?,close_collected_at=?,settled_at=?
                    WHERE report_id=? AND ticker=? AND status='pending'
                    """,
                    (
                        "hit" if hit else "miss",
                        closing["close"],
                        closing["bar_time"],
                        closing["source"],
                        closing["interval"],
                        closing["last_collected_at"],
                        timestamp,
                        forecast["report_id"],
                        forecast["ticker"],
                    ),
                ).rowcount
    return {"resolved": resolved, "reviewed": reviewed, "fetch_failed": fetch_failed}


def attach_market_forecasts(database: Any, reports: list[dict[str, Any]]) -> None:
    pre_reports = {
        report["id"]: report for report in reports if report["report_type"] == "pre_market"
    }
    if not pre_reports:
        return
    placeholders = ",".join("?" for _ in pre_reports)
    jobs = {
        row["report_id"]: dict(row)
        for row in database.execute(
            f"SELECT * FROM market_report_forecast_jobs WHERE report_id IN ({placeholders})",  # noqa: S608
            list(pre_reports),
        ).fetchall()
    }
    forecasts = {
        (row["report_id"], row["ticker"]): dict(row)
        for row in database.execute(
            f"SELECT * FROM market_report_forecasts WHERE report_id IN ({placeholders})",  # noqa: S608
            list(pre_reports),
        ).fetchall()
    }
    for report_id, report in pre_reports.items():
        job = jobs.get(report_id)
        report["forecast_state"] = job["status"] if job else "legacy"
        report["forecast_model"] = json.loads(job["request_json"])["actor"] if job else None
        for leader in report["leaders"]:
            leader["eod_forecast"] = forecasts.get((report_id, leader["ticker"]))
