from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from runner_web.ai_kol import (
    FLASH,
    AIKol,
    actor_snapshot,
    flash_version_snapshot,
    model_display_name,
)
from runner_web.collection import recording_market_data
from runner_web.db import connection
from runner_web.market_clock import market_clock

EASTERN = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
MINIMUM_MOVE_PCT = 0.5
START_PRICE_MAX_AGE = timedelta(minutes=10)
NO_DATA_GRACE = timedelta(days=7)
HEADLINE_SAMPLE = 20
COMPARABLE_SAMPLE = 50
COMPARABLE_TICKERS = 10
COMPARABLE_DAYS = 10
BAR_SOURCE_PRIORITY = {"massive": 0, "yahoo": 1}


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_price(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _is_regular_bar(stamp: datetime) -> bool:
    local = stamp.astimezone(EASTERN)
    local_time = local.time().replace(tzinfo=None)
    return local.weekday() < 5 and REGULAR_OPEN <= local_time < REGULAR_CLOSE


def _latest_regular_bar(ticker: str, at: datetime) -> dict[str, Any] | None:
    with connection() as database:
        rows = database.execute(
            """
            SELECT source,bar_time,close FROM market_bars
            WHERE ticker=? AND interval='5m' AND close>0 AND bar_time<=?
            ORDER BY bar_time DESC LIMIT 600
            """,
            (ticker, _iso(at)),
        ).fetchall()
    for raw in rows:
        stamp = _datetime(raw["bar_time"])
        price = _positive_price(raw["close"])
        if stamp is not None and price is not None and _is_regular_bar(stamp):
            return {"source": str(raw["source"]), "at": stamp, "price": price}
    return None


def prepare_forecast_evidence(
    ticker: str,
    evidence: dict[str, Any],
    *,
    evidence_as_of: str,
) -> dict[str, Any]:


    frozen = dict(evidence)
    as_of = _datetime(evidence_as_of) or datetime.now(UTC)
    quote_at = _datetime(evidence.get("captured_at"))
    quote_price = _positive_price(evidence.get("price"))
    session = market_clock(as_of)["session"]
    selected: dict[str, Any] | None = None
    reason: str | None = None

    if session == "regular":
        if (
            quote_at is not None
            and quote_price is not None
            and timedelta(0) <= as_of - quote_at <= START_PRICE_MAX_AGE
        ):
            selected = {
                "price": quote_price,
                "at": quote_at,
                "source": "runner_snapshot",
            }
        else:
            latest = _latest_regular_bar(ticker, as_of)
            if latest and timedelta(0) <= as_of - latest["at"] <= START_PRICE_MAX_AGE:
                selected = latest
            else:
                reason = "No fresh regular-hours price was available."
    else:
        latest = _latest_regular_bar(ticker, as_of)
        if latest and as_of - latest["at"] <= NO_DATA_GRACE:
            selected = latest
        else:
            reason = "No recent regular-market close was available."

    if selected:
        frozen["price"] = selected["price"]
        frozen["captured_at"] = _iso(selected["at"])
        forecast_start = {
            "eligibility": "eligible",
            "ineligibility_reason": None,
            "price": selected["price"],
            "at": _iso(selected["at"]),
            "source": selected["source"],
            "market_session": session,
        }
    else:
        forecast_start = {
            "eligibility": "unscored",
            "ineligibility_reason": reason or "No reliable start price was available.",
            "price": None,
            "at": None,
            "source": None,
            "market_session": session,
        }
    frozen["forecast_start"] = forecast_start
    frozen["forecast_contract"] = {
        "id": flash_version_snapshot()["forecast_contract_version"],
        "horizon": "next regular session close",
        "minimum_move_pct": MINIMUM_MOVE_PCT,
        "directions": ["up", "down", "no_call"],
        "up_probability_rule": {
            "up_minimum": 0.55,
            "down_maximum": 0.45,
            "no_call_range": [0.45, 0.55],
        },
    }
    return frozen


def validate_forecast(value: Any) -> dict[str, Any]:


    if not isinstance(value, dict):
        raise ValueError("missing forecast")
    direction = str(value.get("direction") or "").strip().lower()
    probability = _number(value.get("probability_up"))
    reason = " ".join(str(value.get("reason") or "").split())
    if direction not in {"up", "down", "no_call"}:
        raise ValueError("invalid forecast direction")
    if probability is None or not 0 <= probability <= 1:
        raise ValueError("invalid forecast probability")
    if not reason:
        raise ValueError("missing forecast reason")
    if direction == "up" and probability < 0.55:
        raise ValueError("up forecast probability is below 55%")
    if direction == "down" and probability > 0.45:
        raise ValueError("down forecast probability is above 45%")
    if direction == "no_call" and not 0.45 <= probability <= 0.55:
        raise ValueError("no-call probability is outside the uncertainty range")
    return {
        "direction": direction,
        "probability_up": round(probability, 6),
        "reason": reason[:500],
    }


def resolved_model_allowed(resolved_model: str, actor: AIKol = FLASH) -> bool:
    version = flash_version_snapshot(actor)
    return resolved_model == version["allowed_resolved_model"]


def _event(
    database: Any,
    forecast_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    at: str,
) -> None:
    database.execute(
        """
        INSERT INTO flash_evaluation_events(
            id,forecast_id,event_type,payload_json,created_at
        ) VALUES(?,?,?,?,?)
        """,
        (
            str(uuid.uuid4()),
            forecast_id,
            event_type,
            json.dumps(payload, separators=(",", ":"), default=str),
            at,
        ),
    )


def record_flash_forecast(
    database: Any,
    report: dict[str, Any],
    generated: dict[str, Any],
    *,
    resolved_model: str,
    usage: dict[str, Any],
    actor: AIKol = FLASH,
    at: str | None = None,
) -> dict[str, Any]:


    timestamp = at or _iso()
    forecast = validate_forecast(generated.get("forecast"))
    version = flash_version_snapshot(actor)
    if str(report.get("flash_version_id") or "") != version["id"]:
        raise ValueError("report Flash version does not match the active release")
    if not resolved_model_allowed(resolved_model, actor):
        raise ValueError("provider returned a model outside the active Flash version")
    evidence = report.get("evidence_snapshot")
    if not isinstance(evidence, dict):
        try:
            evidence = json.loads(report.get("evidence_snapshot_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            evidence = {}
    start = evidence.get("forecast_start")
    if not isinstance(start, dict):
        start = {
            "eligibility": "unscored",
            "ineligibility_reason": "The report did not freeze a forecast start price.",
        }
    eligibility = str(start.get("eligibility") or "unscored")
    if eligibility not in {"eligible", "unscored"}:
        eligibility = "unscored"
    generation = usage.get("generation") if isinstance(usage.get("generation"), dict) else {}
    forecast_id = str(uuid.uuid4())
    database.execute(
        """
        INSERT INTO flash_forecasts(
            id,report_id,version_id,actor_snapshot_json,provider,requested_model,
            resolved_model,provider_request_id,ticker,exchange,evidence_key,evidence_as_of,
            direction,probability_up,reason,start_price,start_at,price_source,
            market_session,contract_version,eligibility,ineligibility_reason,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            forecast_id,
            report["id"],
            version["id"],
            json.dumps(actor_snapshot(actor), separators=(",", ":")),
            actor.provider,
            actor.model,
            resolved_model,
            generation.get("provider_request_id"),
            report["ticker"],
            str(evidence.get("exchange") or ""),
            report["evidence_key"],
            report.get("evidence_as_of") or report["created_at"],
            forecast["direction"],
            forecast["probability_up"],
            forecast["reason"],
            _positive_price(start.get("price")),
            start.get("at"),
            start.get("source"),
            start.get("market_session"),
            version["forecast_contract_version"],
            eligibility,
            start.get("ineligibility_reason"),
            timestamp,
        ),
    )
    if eligibility != "eligible":
        outcome_status = "void"
        void_reason = str(start.get("ineligibility_reason") or "No reliable start price.")[:500]
    elif forecast["direction"] == "no_call":
        outcome_status = "no_call"
        void_reason = None
    else:
        outcome_status = "pending"
        void_reason = None
    database.execute(
        """
        INSERT INTO flash_forecast_outcomes(
            forecast_id,status,void_reason,updated_at
        ) VALUES(?,?,?,?)
        """,
        (forecast_id, outcome_status, void_reason, timestamp),
    )
    _event(
        database,
        forecast_id,
        "created",
        {
            "direction": forecast["direction"],
            "probability_up": forecast["probability_up"],
            "eligibility": eligibility,
            "outcome_status": outcome_status,
        },
        at=timestamp,
    )
    if outcome_status == "void":
        _event(
            database,
            forecast_id,
            "voided",
            {"reason": void_reason},
            at=timestamp,
        )
    return {"id": forecast_id, **forecast, "status": outcome_status}


Bar = tuple[str, datetime, float, float, float]


def _forecast_bars(tickers: list[str]) -> dict[str, list[Bar]]:
    unique = list(dict.fromkeys(tickers))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    with connection() as database:
        rows = database.execute(
            f"""
            SELECT source,ticker,bar_time,high,low,close FROM market_bars
            WHERE interval='5m' AND close>0 AND ticker IN ({placeholders})
            ORDER BY ticker,bar_time,source
            """,
            unique,
        ).fetchall()
    selected: dict[str, dict[datetime, Bar]] = {}
    for raw in rows:
        stamp = _datetime(raw["bar_time"])
        close = _positive_price(raw["close"])
        if stamp is None or close is None or not _is_regular_bar(stamp):
            continue
        high = _positive_price(raw["high"]) or close
        low = _positive_price(raw["low"]) or close
        ticker = str(raw["ticker"])
        candidate = (str(raw["source"]), stamp, high, low, close)
        existing = selected.setdefault(ticker, {}).get(stamp)
        candidate_rank = (BAR_SOURCE_PRIORITY.get(candidate[0], 100), candidate[0])
        existing_rank = (
            (BAR_SOURCE_PRIORITY.get(existing[0], 100), existing[0])
            if existing
            else None
        )
        if existing_rank is None or candidate_rank < existing_rank:
            selected[ticker][stamp] = candidate
    return {
        ticker: sorted(bars.values(), key=lambda item: item[1])
        for ticker, bars in selected.items()
    }


def _corporate_action(
    database: Any,
    ticker: str,
    start_at: str,
    observed_at: str,
) -> dict[str, Any] | None:
    row = database.execute(
        """
        SELECT event_type,event_at,payload_json FROM market_events
        WHERE ticker=? AND event_at>? AND event_at<=?
          AND event_type IN ('reverse_split','corporate_action','security_action')
        ORDER BY event_at LIMIT 1
        """,
        (ticker, start_at, observed_at),
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    event_type = str(row["event_type"])
    action_text = str(
        payload.get("action_type") or payload.get("type") or payload.get("description") or ""
    ).lower()
    is_split = event_type == "reverse_split" or "reverse split" in action_text
    return {
        "event_type": event_type,
        "event_at": str(row["event_at"]),
        "is_split": is_split,
    }


def refresh_flash_forecasts(
    at: datetime | None = None,
    *,
    fetch_market_data: bool = True,
) -> dict[str, int]:


    current = (at or datetime.now(UTC)).astimezone(UTC)
    timestamp = _iso(current)
    with connection() as database:
        pending = [
            dict(row)
            for row in database.execute(
                """
                SELECT f.*,o.status AS outcome_status FROM flash_forecasts f
                JOIN flash_forecast_outcomes o ON o.forecast_id=f.id
                WHERE o.status='pending' AND f.eligibility='eligible'
                ORDER BY f.created_at
                """
            ).fetchall()
        ]
    tickers = list(dict.fromkeys(str(row["ticker"]) for row in pending))
    if fetch_market_data and tickers:
        try:
            with recording_market_data(batch_size=60) as market_data:
                market_data.intraday(tickers)
        except Exception:

            pass
    bars_by_ticker = _forecast_bars(tickers)
    resolved = 0
    voided = 0
    reviewed = 0
    checked = 0
    with connection() as database:
        for forecast in pending:
            checked += 1
            start_at = _datetime(forecast.get("start_at"))
            start_price = _positive_price(forecast.get("start_price"))
            if start_at is None or start_price is None:
                continue
            start_date = start_at.astimezone(EASTERN).date()
            sessions: dict[Any, list[Bar]] = {}
            for bar in bars_by_ticker.get(str(forecast["ticker"]), []):
                session_date = bar[1].astimezone(EASTERN).date()
                if session_date > start_date:
                    sessions.setdefault(session_date, []).append(bar)
            if not sessions:
                if current - start_at > NO_DATA_GRACE:
                    updated = database.execute(
                        """
                        UPDATE flash_forecast_outcomes
                        SET status='void',void_reason=?,
                            first_checked_at=COALESCE(first_checked_at,?),
                            resolved_at=?,updated_at=?
                        WHERE forecast_id=? AND status='pending'
                        """,
                        (
                            "No reliable next-session closing price was available.",
                            timestamp,
                            timestamp,
                            timestamp,
                            forecast["id"],
                        ),
                    ).rowcount
                    if updated:
                        _event(
                            database,
                            str(forecast["id"]),
                            "voided",
                            {"reason": "void_no_data"},
                            at=timestamp,
                        )
                        voided += 1
                continue
            target_date = min(sessions)
            current_local = current.astimezone(EASTERN)
            if target_date > current_local.date():
                continue
            if target_date == current_local.date() and current_local.time() < REGULAR_CLOSE:
                continue
            session_bars = sorted(sessions[target_date], key=lambda item: item[1])
            source, observed_at, _high, _low, end_price = session_bars[-1]
            observed_iso = _iso(observed_at)
            action = _corporate_action(
                database,
                str(forecast["ticker"]),
                str(forecast["start_at"]),
                observed_iso,
            )
            if action:
                status = "void" if action["is_split"] else "under_review"
                event_type = "voided" if action["is_split"] else "reviewed"
                reason = (
                    "A reverse split changed the price basis."
                    if action["is_split"]
                    else "A corporate action needs price-basis review."
                )
                updated = database.execute(
                    """
                    UPDATE flash_forecast_outcomes
                    SET status=?,corporate_action_state=?,void_reason=?,
                        first_checked_at=COALESCE(first_checked_at,?),resolved_at=?,updated_at=?
                    WHERE forecast_id=? AND status='pending'
                    """,
                    (
                        status,
                        action["event_type"],
                        reason,
                        timestamp,
                        timestamp,
                        timestamp,
                        forecast["id"],
                    ),
                ).rowcount
                if updated:
                    database.execute(
                        "UPDATE flash_forecasts SET target_session_date=? WHERE id=?",
                        (target_date.isoformat(), forecast["id"]),
                    )
                    _event(database, str(forecast["id"]), event_type, action, at=timestamp)
                    if status == "void":
                        voided += 1
                    else:
                        reviewed += 1
                continue
            return_pct = round((end_price / start_price - 1.0) * 100.0, 4)
            direction = str(forecast["direction"])
            signed_move = round(return_pct if direction == "up" else -return_pct, 4)
            classification = "hit" if signed_move > MINIMUM_MOVE_PCT else "miss"
            if classification == "miss":
                miss_reason = (
                    "wrong_way"
                    if signed_move < -MINIMUM_MOVE_PCT
                    else "no_meaningful_move"
                )
            else:
                miss_reason = None
            maximum = max(bar[2] for bar in session_bars)
            minimum = min(bar[3] for bar in session_bars)
            maximum_return = round((maximum / start_price - 1.0) * 100.0, 4)
            minimum_return = round((minimum / start_price - 1.0) * 100.0, 4)
            if direction == "up":
                maximum_favorable = maximum_return
                maximum_adverse = minimum_return
            else:
                maximum_favorable = -minimum_return
                maximum_adverse = -maximum_return
            fingerprint = hashlib.sha256(
                json.dumps(
                    [
                        [bar[0], _iso(bar[1]), bar[2], bar[3], bar[4]]
                        for bar in session_bars
                    ],
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            updated = database.execute(
                """
                UPDATE flash_forecast_outcomes SET
                    status='resolved',classification=?,miss_reason=?,end_price=?,observed_at=?,
                    return_pct=?,signed_move_pct=?,max_favorable_pct=?,max_adverse_pct=?,
                    bar_source=?,bar_fingerprint=?,first_checked_at=COALESCE(first_checked_at,?),
                    resolved_at=?,updated_at=?
                WHERE forecast_id=? AND status='pending'
                """,
                (
                    classification,
                    miss_reason,
                    end_price,
                    observed_iso,
                    return_pct,
                    signed_move,
                    maximum_favorable,
                    maximum_adverse,
                    source,
                    fingerprint,
                    timestamp,
                    timestamp,
                    timestamp,
                    forecast["id"],
                ),
            ).rowcount
            if not updated:
                continue
            database.execute(
                "UPDATE flash_forecasts SET target_session_date=? WHERE id=?",
                (target_date.isoformat(), forecast["id"]),
            )
            _event(
                database,
                str(forecast["id"]),
                "resolved",
                {
                    "classification": classification,
                    "miss_reason": miss_reason,
                    "return_pct": return_pct,
                    "signed_move_pct": signed_move,
                    "observed_at": observed_iso,
                },
                at=timestamp,
            )
            resolved += 1
    return {
        "pending": len(pending),
        "checked": checked,
        "resolved": resolved,
        "voided": voided,
        "reviewed": reviewed,
    }


def correct_flash_outcome(
    forecast_id: str,
    *,
    reason: str,
    end_price: float | None = None,
    observed_at: str | None = None,
    void: bool = False,
    at: datetime | None = None,
) -> dict[str, Any]:


    clean_reason = " ".join(reason.split())[:500]
    if not clean_reason:
        raise ValueError("a correction reason is required")
    timestamp = _iso(at)
    with connection() as database:
        raw = database.execute(
            """
            SELECT f.direction,f.start_price,o.* FROM flash_forecasts f
            JOIN flash_forecast_outcomes o ON o.forecast_id=f.id
            WHERE f.id=?
            """,
            (forecast_id,),
        ).fetchone()
        if not raw:
            raise KeyError("Flash forecast not found")
        row = dict(raw)
        if str(row["direction"]) == "no_call":
            raise ValueError("a no-call forecast has no market outcome to correct")
        previous = {
            key: row.get(key)
            for key in (
                "status",
                "classification",
                "miss_reason",
                "end_price",
                "observed_at",
                "return_pct",
                "signed_move_pct",
                "bar_source",
                "bar_fingerprint",
                "corporate_action_state",
                "void_reason",
                "resolved_at",
            )
        }
        if void:
            current = {
                "status": "void",
                "classification": None,
                "miss_reason": None,
                "end_price": None,
                "observed_at": observed_at,
                "return_pct": None,
                "signed_move_pct": None,
                "void_reason": clean_reason,
            }
        else:
            start_price = _positive_price(row.get("start_price"))
            corrected_end = _positive_price(end_price)
            observed = _datetime(observed_at)
            if start_price is None or corrected_end is None or observed is None:
                raise ValueError("a corrected end price and observation time are required")
            return_pct = round((corrected_end / start_price - 1.0) * 100.0, 4)
            signed_move = round(
                return_pct if str(row["direction"]) == "up" else -return_pct,
                4,
            )
            classification = "hit" if signed_move > MINIMUM_MOVE_PCT else "miss"
            miss_reason = None
            if classification == "miss":
                miss_reason = (
                    "wrong_way"
                    if signed_move < -MINIMUM_MOVE_PCT
                    else "no_meaningful_move"
                )
            current = {
                "status": "resolved",
                "classification": classification,
                "miss_reason": miss_reason,
                "end_price": corrected_end,
                "observed_at": _iso(observed),
                "return_pct": return_pct,
                "signed_move_pct": signed_move,
                "void_reason": None,
            }
        database.execute(
            """
            UPDATE flash_forecast_outcomes SET
                status=?,classification=?,miss_reason=?,end_price=?,observed_at=?,
                return_pct=?,signed_move_pct=?,max_favorable_pct=NULL,max_adverse_pct=NULL,
                bar_source='manual_correction',bar_fingerprint=NULL,
                corporate_action_state=NULL,void_reason=?,resolved_at=?,updated_at=?
            WHERE forecast_id=?
            """,
            (
                current["status"],
                current["classification"],
                current["miss_reason"],
                current["end_price"],
                current["observed_at"],
                current["return_pct"],
                current["signed_move_pct"],
                current["void_reason"],
                timestamp,
                timestamp,
                forecast_id,
            ),
        )
        _event(
            database,
            forecast_id,
            "corrected",
            {"reason": clean_reason, "previous": previous, "current": current},
            at=timestamp,
        )
    return {"forecast_id": forecast_id, **current, "correction_reason": clean_reason}


def _wilson_interval(successes: int, total: int) -> list[float] | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1.0 + z**2 / total
    center = (proportion + z**2 / (2 * total)) / denominator
    margin = (
        z
        * ((proportion * (1 - proportion) + z**2 / (4 * total)) / total) ** 0.5
        / denominator
    )
    return [round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4)]


def _version_scorecard(
    version: dict[str, Any],
    forecasts: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    resolved = [row for row in forecasts if row.get("outcome_status") == "resolved"]
    hits = sum(row.get("classification") == "hit" for row in resolved)
    misses = len(resolved) - hits
    pending = sum(row.get("outcome_status") == "pending" for row in forecasts)
    no_calls = sum(row.get("outcome_status") == "no_call" for row in forecasts)
    voids = sum(row.get("outcome_status") == "void" for row in forecasts)
    under_review = sum(row.get("outcome_status") == "under_review" for row in forecasts)
    eligible = [row for row in forecasts if row.get("eligibility") == "eligible"]
    directional = [row for row in eligible if row.get("direction") in {"up", "down"}]
    clear = [
        row
        for row in resolved
        if row.get("return_pct") is not None and abs(float(row["return_pct"])) > MINIMUM_MOVE_PCT
    ]
    brier = None
    if clear:
        brier = sum(
            (float(row["probability_up"]) - float(float(row["return_pct"]) > 0)) ** 2
            for row in clear
        ) / len(clear)
    trading_days = {
        str(row.get("target_session_date"))
        for row in resolved
        if row.get("target_session_date")
    }
    tickers = {str(row["ticker"]) for row in resolved}
    settled = len(resolved)
    comparable = (
        settled >= COMPARABLE_SAMPLE
        and len(tickers) >= COMPARABLE_TICKERS
        and len(trading_days) >= COMPARABLE_DAYS
    )
    if version["status"] == "retired":
        state = "retired"
    elif comparable:
        state = "comparable"
    elif settled >= HEADLINE_SAMPLE:
        state = "early"
    else:
        state = "building"
    completed_attempts = sum(row.get("status") == "complete" for row in attempts)
    failed_attempts = sum(row.get("status") == "failed" for row in attempts)
    finished_attempts = completed_attempts + failed_attempts
    signed_moves = sorted(
        float(row["signed_move_pct"])
        for row in resolved
        if row.get("signed_move_pct") is not None
    )
    median_signed_move = None
    if signed_moves:
        middle = len(signed_moves) // 2
        median_signed_move = (
            signed_moves[middle]
            if len(signed_moves) % 2
            else (signed_moves[middle - 1] + signed_moves[middle]) / 2
        )
    return {
        "id": version["id"],
        "label": version["public_label"],
        "status": version["status"],
        "state": state,
        "model": version["requested_model"],
        "model_label": model_display_name(str(version["requested_model"])),
        "launched_at": version["launched_at"],
        "retired_at": version.get("retired_at"),
        "prompt_version": version["prompt_version"],
        "context_version": version["context_version"],
        "risk_policy_version": version["risk_policy_version"],
        "output_schema_version": version["output_schema_version"],
        "contract_version": version["forecast_contract_version"],
        "hits": hits,
        "misses": misses,
        "pending": pending,
        "no_calls": no_calls,
        "voids": voids,
        "under_review": under_review,
        "settled": settled,
        "hit_rate": round(hits / settled, 4) if settled else None,
        "hit_rate_interval_95": _wilson_interval(hits, settled),
        "headline_rate_visible": settled >= HEADLINE_SAMPLE,
        "distinct_tickers": len(tickers),
        "distinct_trading_days": len(trading_days),
        "forecast_coverage": round(len(directional) / len(eligible), 4) if eligible else None,
        "median_signed_move_pct": (
            round(median_signed_move, 4) if median_signed_move is not None else None
        ),
        "brier_score": round(brier, 4) if brier is not None else None,
        "reports_completed": completed_attempts,
        "reports_failed": failed_attempts,
        "completion_rate": (
            round(completed_attempts / finished_attempts, 4) if finished_attempts else None
        ),
    }


def _display_forecast(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "report_id": row["report_id"],
        "report_public_id": row.get("report_public_id"),
        "report_url": (
            f"/research/{row['report_public_id']}" if row.get("report_public_id") else None
        ),
        "version_id": row["version_id"],
        "version_label": row.get("version_label"),
        "ticker": row["ticker"],
        "direction": row["direction"],
        "probability_up": row["probability_up"],
        "reason": row["reason"],
        "start_price": row.get("start_price"),
        "start_at": row.get("start_at"),
        "target_session_date": row.get("target_session_date"),
        "eligibility": row["eligibility"],
        "ineligibility_reason": row.get("ineligibility_reason"),
        "status": row.get("outcome_status"),
        "classification": row.get("classification"),
        "miss_reason": row.get("miss_reason"),
        "end_price": row.get("end_price"),
        "observed_at": row.get("observed_at"),
        "return_pct": row.get("return_pct"),
        "signed_move_pct": row.get("signed_move_pct"),
        "void_reason": row.get("void_reason"),
        "correction_count": int(row.get("correction_count") or 0),
        "created_at": row["created_at"],
    }


def forecast_for_report(report_id: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            """
            SELECT f.*,v.public_label AS version_label,o.status AS outcome_status,
                   o.classification,o.miss_reason,o.end_price,o.observed_at,o.return_pct,
                   o.signed_move_pct,o.void_reason
            FROM flash_forecasts f
            JOIN flash_versions v ON v.id=f.version_id
            JOIN flash_forecast_outcomes o ON o.forecast_id=f.id
            WHERE f.report_id=?
            """,
            (report_id,),
        ).fetchone()
    if not row:
        return None
    display = _display_forecast(dict(row))
    with connection() as database:
        corrections = database.execute(
            """
            SELECT payload_json,created_at FROM flash_evaluation_events
            WHERE forecast_id=? AND event_type='corrected'
            ORDER BY created_at
            """,
            (row["id"],),
        ).fetchall()
    display["corrections"] = []
    for correction in corrections:
        try:
            payload = json.loads(correction["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        display["corrections"].append(
            {
                "reason": str(payload.get("reason") or "Result corrected."),
                "previous": payload.get("previous"),
                "current": payload.get("current"),
                "created_at": str(correction["created_at"]),
            }
        )
    display["correction_count"] = len(display["corrections"])
    return display


def flash_record(*, recent_limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(recent_limit, 100))
    with connection() as database:
        versions = [
            dict(row)
            for row in database.execute(
                """
                SELECT * FROM flash_versions
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,launched_at DESC
                """
            ).fetchall()
        ]
        forecasts = [
            dict(row)
            for row in database.execute(
                """
                SELECT f.*,r.public_id AS report_public_id,r.visibility,
                       v.public_label AS version_label,
                       o.status AS outcome_status,o.classification,o.miss_reason,o.end_price,
                       o.observed_at,o.return_pct,o.signed_move_pct,o.void_reason,
                       (SELECT COUNT(*) FROM flash_evaluation_events e
                        WHERE e.forecast_id=f.id AND e.event_type='corrected')
                           AS correction_count
                FROM flash_forecasts f
                JOIN research_commissions r ON r.id=f.report_id
                JOIN flash_versions v ON v.id=f.version_id
                JOIN flash_forecast_outcomes o ON o.forecast_id=f.id
                WHERE r.status='complete'
                ORDER BY f.created_at DESC
                """
            ).fetchall()
        ]
        attempts = [
            dict(row)
            for row in database.execute(
                """
                SELECT flash_version_id,status FROM research_commissions
                WHERE flash_version_id IS NOT NULL
                """
            ).fetchall()
        ]
    by_version: dict[str, list[dict[str, Any]]] = {}
    for row in forecasts:
        by_version.setdefault(str(row["version_id"]), []).append(row)
    attempts_by_version: dict[str, list[dict[str, Any]]] = {}
    for row in attempts:
        attempts_by_version.setdefault(str(row["flash_version_id"]), []).append(row)
    scorecards = [
        _version_scorecard(
            version,
            by_version.get(str(version["id"]), []),
            attempts_by_version.get(str(version["id"]), []),
        )
        for version in versions
    ]
    current = next((row for row in scorecards if row["status"] == "active"), None)
    public_rows = [row for row in forecasts if row.get("visibility") == "public"][:limit]
    return {
        "contract": {
            "id": flash_version_snapshot()["forecast_contract_version"],
            "horizon_label": "next regular session close",
            "minimum_move_pct": MINIMUM_MOVE_PCT,
            "headline_sample": HEADLINE_SAMPLE,
            "comparable_sample": COMPARABLE_SAMPLE,
        },
        "current_version": current,
        "versions": scorecards,
        "recent_results": [_display_forecast(row) for row in public_rows],
        "method_note": (
            "This record judges frozen price forecasts on user-selected reports. "
            "It does not verify every fact in a report."
        ),
    }
