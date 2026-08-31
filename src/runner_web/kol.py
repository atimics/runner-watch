from __future__ import annotations

import json
import math
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from runner_web.ai_kol import KOL_LADDER_SIZE, model_display_name
from runner_web.collection import recording_market_data
from runner_web.db import connection
from runner_web.outcomes import _bar_prices, barrier_outcome
from runner_web.research_policy import research_policy_scorecards


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _return_pct(base_price: float, later_price: float) -> float:
    return round((later_price / base_price - 1.0) * 100.0, 4)


def _valid_price(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _actor_snapshot_from_predictor(predictor: dict[str, Any]) -> dict[str, Any]:
    model = str(predictor.get("inference_model") or "")
    return {
        "id": str(predictor["id"]),
        "slot": str(predictor.get("slot") or predictor.get("slug") or ""),
        "ladder_position": predictor.get("ladder_position"),
        "ladder_size": KOL_LADDER_SIZE,
        "display_name": str(predictor["display_name"]),
        "emoji": str(predictor["emoji"]),
        "provider": str(predictor.get("inference_provider") or ""),
        "model": model,
        "model_label": model_display_name(model),
        "description": str(predictor.get("description") or ""),
        "authorship": "deterministic_signal_policy",
    }


def _event(
    db: Any,
    call_id: str,
    event_type: str,
    event_at: str,
    *,
    price: float | None = None,
    return_pct: float | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO kol_call_events(
            call_id,event_type,event_at,price,return_pct,payload_json
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            call_id,
            event_type,
            event_at,
            price,
            return_pct,
            json.dumps(payload or {}, separators=(",", ":")),
        ),
    )


def _close_call(
    db: Any,
    call: dict[str, Any],
    *,
    status: str,
    price: float,
    closed_at: str,
    reason: str,
) -> None:
    gross_return = _return_pct(float(call["entry_price"]), price)
    net_return = round(gross_return - float(call["round_trip_cost_bps"]) / 100.0, 4)
    paper_pnl = round(float(call["paper_notional"]) * net_return / 100.0, 2)
    db.execute(
        """
        UPDATE kol_calls SET
            status=?,last_price=?,last_mark_at=?,unrealized_return_pct=?,
            exit_price=?,exit_at=?,realized_return_pct=?,net_return_pct=?,
            paper_pnl=?,close_reason=?,
            max_favorable_pct=MAX(max_favorable_pct,?),
            max_adverse_pct=MIN(max_adverse_pct,?),updated_at=?
        WHERE id=? AND status='active'
        """,
        (
            status,
            price,
            closed_at,
            gross_return,
            price,
            closed_at,
            gross_return,
            net_return,
            paper_pnl,
            reason,
            gross_return,
            gross_return,
            closed_at,
            call["id"],
        ),
    )
    _event(
        db,
        str(call["id"]),
        status,
        closed_at,
        price=price,
        return_pct=net_return,
        payload={"reason": reason, "gross_return_pct": gross_return},
    )


def publish_calls_for_scan(
    scan_run_id: str,
    model_id: str,
    *,
    predictor_slug: str | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Turn selective ranker predictions into immutable, public call receipts."""

    timestamp = _iso(at)
    created: list[str] = []
    abandoned: list[str] = []
    with connection() as db:
        if predictor_slug:
            predictors = db.execute(
                "SELECT * FROM kol_predictors WHERE slug=? AND status!='paused'",
                (predictor_slug,),
            ).fetchall()
        else:
            predictors = db.execute(
                "SELECT * FROM kol_predictors WHERE status='champion'"
            ).fetchall()
        predictions = [
            dict(row)
            for row in db.execute(
                """
                SELECT p.*,s.ticker,s.price,s.captured_at,s.scan_run_id,
                       s.rug_score,s.rug_level,s.trade_state,s.hard_veto
                FROM ranker_predictions p
                JOIN scan_snapshots s ON s.id=p.snapshot_id
                WHERE s.scan_run_id=? AND p.model_id=?
                ORDER BY p.rank,s.ticker
                """,
                (scan_run_id, model_id),
            ).fetchall()
        ]
        predictions_by_ticker = {str(row["ticker"]): row for row in predictions}

        for raw_predictor in predictors:
            predictor = dict(raw_predictor)
            active_rows = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM kol_calls WHERE predictor_id=? AND status='active'",
                    (predictor["id"],),
                ).fetchall()
            ]
            for call in active_rows:
                prediction = predictions_by_ticker.get(str(call["ticker"]))
                if not prediction:
                    continue
                probability = float(prediction.get("probability_up") or 0.0)
                expected = float(prediction.get("expected_return_pct") or 0.0)
                risk_blocks = (
                    bool(prediction.get("hard_veto"))
                    or float(prediction.get("rug_score") or 0.0) >= 65
                    or str(prediction.get("trade_state") or "").upper() in {"AVOID", "EXIT"}
                )
                should_abandon = (
                    probability <= float(predictor["abandon_probability_up"])
                    or expected <= float(predictor["abandon_expected_return_pct"])
                    or risk_blocks
                )
                if not should_abandon:
                    continue
                price = _valid_price(prediction.get("price"))
                if price is None:
                    continue
                _close_call(
                    db,
                    call,
                    status="abandoned",
                    price=price,
                    closed_at=str(prediction["captured_at"]),
                    reason="Score fell below the exit rule.",
                )
                abandoned.append(str(call["id"]))

            active_count = int(
                db.execute(
                    "SELECT COUNT(*) FROM kol_calls WHERE predictor_id=? AND status='active'",
                    (predictor["id"],),
                ).fetchone()[0]
            )
            room = max(0, int(predictor["max_active_calls"]) - active_count)
            allowance = min(room, int(predictor["max_calls_per_scan"]))
            if allowance <= 0:
                continue

            predictor_created = 0
            for prediction in predictions:
                if predictor_created >= allowance:
                    break
                probability = float(prediction.get("probability_up") or 0.0)
                expected = float(prediction.get("expected_return_pct") or 0.0)
                price = _valid_price(prediction.get("price"))
                raw_trade_state = prediction.get("trade_state")
                trade_state = str(raw_trade_state).upper() if raw_trade_state else ""
                if (
                    probability < float(predictor["min_probability_up"])
                    or expected < float(predictor["min_expected_return_pct"])
                    or price is None
                    or bool(prediction.get("hard_veto"))
                    or float(prediction.get("rug_score") or 0.0) >= 50
                    or (trade_state and trade_state not in {"TRIGGERED", "MANAGE"})
                ):
                    continue
                if db.execute(
                    """
                    SELECT 1 FROM kol_calls
                    WHERE predictor_id=? AND ticker=? AND status='active'
                    """,
                    (predictor["id"], prediction["ticker"]),
                ).fetchone():
                    continue
                call_id = f"call-{secrets.token_urlsafe(12)}"
                call_at = str(prediction["captured_at"])
                call_actor = _actor_snapshot_from_predictor(predictor)
                inserted = db.execute(
                    """
                    INSERT OR IGNORE INTO kol_calls(
                        id,predictor_id,model_id,actor_snapshot_json,
                        snapshot_id,scan_run_id,ticker,
                        contract_version,upper_barrier_pct,lower_barrier_pct,
                        horizon_minutes,status,confidence,expected_return_pct,
                        entry_price,entry_at,last_price,last_mark_at,
                        unrealized_return_pct,paper_notional,round_trip_cost_bps,
                        paper_pnl,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        call_id,
                        predictor["id"],
                        model_id,
                        json.dumps(call_actor, separators=(",", ":")),
                        prediction["snapshot_id"],
                        scan_run_id,
                        prediction["ticker"],
                        predictor["contract_version"],
                        predictor["upper_barrier_pct"],
                        predictor["lower_barrier_pct"],
                        predictor["horizon_minutes"],
                        "active",
                        probability,
                        expected,
                        price,
                        call_at,
                        price,
                        call_at,
                        0.0,
                        predictor["paper_notional"],
                        predictor["round_trip_cost_bps"],
                        round(
                            -float(predictor["paper_notional"])
                            * float(predictor["round_trip_cost_bps"])
                            / 10_000.0,
                            2,
                        ),
                        timestamp,
                        timestamp,
                    ),
                ).rowcount
                if not inserted:
                    continue
                _event(
                    db,
                    call_id,
                    "called",
                    call_at,
                    price=price,
                    payload={
                        "confidence": probability,
                        "expected_return_pct": expected,
                        "model_rank": prediction["rank"],
                        "model_id": model_id,
                        "actor": call_actor,
                        "contract_version": predictor["contract_version"],
                    },
                )
                created.append(call_id)
                predictor_created += 1

    return {
        "scan_run_id": scan_run_id,
        "model_id": model_id,
        "calls_created": len(created),
        "calls_abandoned": len(abandoned),
        "created_ids": created,
        "abandoned_ids": abandoned,
    }


def _latest_prices(tickers: list[str]) -> dict[str, float]:
    if not tickers:
        return {}
    with recording_market_data(batch_size=60) as market_data:
        result = market_data.intraday(list(dict.fromkeys(tickers)))
    prices: dict[str, float] = {}
    for ticker, frame in result.frames.items():
        close = pd.Series(dtype="float64")
        for column in frame.columns:
            if str(column).lower().replace(" ", "") == "close":
                close = pd.to_numeric(frame[column], errors="coerce").dropna()
                break
        if not close.empty:
            price = _valid_price(close.iloc[-1])
            if price is not None:
                prices[str(ticker)] = price
    return prices


def refresh_kol_calls(
    at: datetime | None = None,
    *,
    latest_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Mark active paper calls and copy the fixed scanner outcome onto every receipt."""

    timestamp = _iso(at)
    with connection() as db:
        active = [
            dict(row)
            for row in db.execute("SELECT * FROM kol_calls WHERE status='active'").fetchall()
        ]
    prices = (
        latest_prices
        if latest_prices is not None
        else _latest_prices([str(call["ticker"]) for call in active])
    )
    archived_bars = _bar_prices([str(call["ticker"]) for call in active])

    marked = 0
    closed = 0
    benchmarked = 0
    with connection() as db:
        outcomes = {
            str(row["snapshot_id"]): dict(row)
            for row in db.execute(
                """
                SELECT snapshot_id,barrier_label,barrier_hit_at,price_60m,
                       return_60m_pct,observed_60m_at,max_favorable_pct,
                       max_adverse_pct,updated_at
                FROM scan_outcomes
                WHERE snapshot_id IN (SELECT snapshot_id FROM kol_calls)
                      AND barrier_label IS NOT NULL
                """
            ).fetchall()
        }
        for call in active:
            price = _valid_price(prices.get(str(call["ticker"])))
            if price is not None:
                gross_return = _return_pct(float(call["entry_price"]), price)
                net_return = round(gross_return - float(call["round_trip_cost_bps"]) / 100.0, 4)
                paper_pnl = round(float(call["paper_notional"]) * net_return / 100.0, 2)
                db.execute(
                    """
                    UPDATE kol_calls SET last_price=?,last_mark_at=?,
                        unrealized_return_pct=?,paper_pnl=?,
                        max_favorable_pct=MAX(max_favorable_pct,?),
                        max_adverse_pct=MIN(max_adverse_pct,?),updated_at=?
                    WHERE id=? AND status='active'
                    """,
                    (
                        price,
                        timestamp,
                        gross_return,
                        paper_pnl,
                        gross_return,
                        gross_return,
                        timestamp,
                        call["id"],
                    ),
                )
                call.update(
                    last_price=price,
                    last_mark_at=timestamp,
                    unrealized_return_pct=gross_return,
                    paper_pnl=paper_pnl,
                )
                marked += 1

            outcome = outcomes.get(str(call["snapshot_id"]))
            if not outcome:
                try:
                    entry_at = datetime.fromisoformat(str(call["entry_at"]))
                except ValueError:
                    entry_at = None
                if entry_at is not None:
                    if entry_at.tzinfo is None:
                        entry_at = entry_at.replace(tzinfo=UTC)
                    ticker_bars = archived_bars.get(str(call["ticker"]), [])
                    live_barrier = barrier_outcome(
                        ticker_bars,
                        entry_at,
                        float(call["entry_price"]),
                    )
                    if live_barrier:
                        window = [
                            bar
                            for bar in ticker_bars
                            if entry_at.astimezone(UTC)
                            < bar[0]
                            <= entry_at.astimezone(UTC)
                            + timedelta(minutes=int(call["horizon_minutes"]))
                        ]
                        observed_price = window[-1][3] if window else call["last_price"]
                        observed_at = _iso(window[-1][0]) if window else timestamp
                        outcome = {
                            **live_barrier,
                            "price_60m": observed_price,
                            "return_60m_pct": _return_pct(
                                float(call["entry_price"]), float(observed_price)
                            ),
                            "observed_60m_at": observed_at,
                            "updated_at": timestamp,
                        }
                        outcomes[str(call["snapshot_id"])] = outcome
            if not outcome:
                continue
            label = str(outcome["barrier_label"])
            if label == "up":
                exit_price = float(call["entry_price"]) * (
                    1.0 + float(call["upper_barrier_pct"]) / 100.0
                )
                status = "won"
                reason = "Hit the profit target first."
            elif label == "down":
                exit_price = float(call["entry_price"]) * (
                    1.0 - float(call["lower_barrier_pct"]) / 100.0
                )
                status = "stopped"
                reason = "Hit the stop first."
            else:
                exit_price = _valid_price(outcome.get("price_60m")) or float(call["last_price"])
                status = "timed_out"
                reason = "Ended after 60 minutes."
            closed_at = str(
                outcome.get("barrier_hit_at")
                or outcome.get("observed_60m_at")
                or outcome["updated_at"]
            )
            _close_call(
                db,
                call,
                status=status,
                price=exit_price,
                closed_at=closed_at,
                reason=reason,
            )
            closed += 1

        unsettled = db.execute(
            "SELECT id,snapshot_id FROM kol_calls WHERE benchmark_label IS NULL"
        ).fetchall()
        for row in unsettled:
            outcome = outcomes.get(str(row["snapshot_id"]))
            if not outcome:
                continue
            benchmark_at = str(
                outcome.get("barrier_hit_at")
                or outcome.get("observed_60m_at")
                or outcome["updated_at"]
            )
            db.execute(
                """
                UPDATE kol_calls SET benchmark_label=?,benchmark_return_60m_pct=?,
                    benchmark_at=?,
                    max_favorable_pct=MAX(max_favorable_pct,COALESCE(?,0)),
                    max_adverse_pct=MIN(max_adverse_pct,COALESCE(?,0)),updated_at=?
                WHERE id=? AND benchmark_label IS NULL
                """,
                (
                    outcome["barrier_label"],
                    outcome.get("return_60m_pct"),
                    benchmark_at,
                    outcome.get("max_favorable_pct"),
                    outcome.get("max_adverse_pct"),
                    timestamp,
                    row["id"],
                ),
            )
            _event(
                db,
                str(row["id"]),
                "benchmarked",
                benchmark_at,
                return_pct=outcome.get("return_60m_pct"),
                payload={"label": outcome["barrier_label"]},
            )
            benchmarked += 1

    return {"active": len(active), "marked": marked, "closed": closed, "benchmarked": benchmarked}


def _display_call(raw: Any) -> dict[str, Any]:
    call = dict(raw)
    call["signal_model_id"] = call.get("model_id")
    try:
        actor = json.loads(call.get("actor_snapshot_json") or "{}")
    except (TypeError, ValueError):
        actor = {}
    if not isinstance(actor, dict):
        actor = {}
    call["actor"] = actor
    call["authorship"] = str(actor.get("authorship") or "unknown")
    for key in (
        "slot",
        "ladder_position",
        "inference_provider",
        "inference_model",
    ):
        actor_key = key.removeprefix("inference_")
        if actor.get(actor_key) is not None:
            call[key] = actor[actor_key]
    call["inference_model_label"] = str(
        actor.get("model_label") or model_display_name(str(call.get("inference_model") or ""))
    )
    gross = (
        call.get("realized_return_pct")
        if call.get("realized_return_pct") is not None
        else call.get("unrealized_return_pct")
    )
    net = (
        call.get("net_return_pct")
        if call.get("net_return_pct") is not None
        else round(float(gross or 0.0) - float(call["round_trip_cost_bps"]) / 100.0, 4)
    )
    call["display_return_pct"] = net
    call["is_active"] = call["status"] == "active"
    return call


def calls_for_tickers(
    tickers: list[str], *, active_only: bool = True, limit_per_ticker: int = 5
) -> dict[str, list[dict[str, Any]]]:
    unique = list(dict.fromkeys(str(ticker) for ticker in tickers))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    status_clause = "AND c.status='active'" if active_only else ""
    with connection() as db:
        rows = db.execute(
            f"""
            SELECT c.*,p.slug AS predictor_slug,p.display_name,p.emoji,p.visible,
                   p.slot,p.ladder_position,p.inference_provider,p.inference_model,
                   p.upper_barrier_pct,p.lower_barrier_pct,p.horizon_minutes
            FROM kol_calls c
            JOIN kol_predictors p ON p.id=c.predictor_id
            WHERE c.ticker IN ({placeholders}) {status_clause}
                  AND p.visible=1
            ORDER BY c.ticker,CASE c.status WHEN 'active' THEN 0 ELSE 1 END,
                     c.created_at DESC
            """,  # noqa: S608
            unique,
        ).fetchall()
    output: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        ticker = str(raw["ticker"])
        bucket = output.setdefault(ticker, [])
        if len(bucket) < max(1, limit_per_ticker):
            bucket.append(_display_call(raw))
    return output


def calls_for_ticker(ticker: str, limit: int = 20) -> list[dict[str, Any]]:
    return calls_for_tickers(
        [ticker], active_only=False, limit_per_ticker=max(1, min(limit, 100))
    ).get(ticker, [])


def predictor_scorecards(*, include_hidden: bool = False) -> list[dict[str, Any]]:
    with connection() as db:
        predictors = db.execute(
            """
            WITH totals AS (
                SELECT predictor_id,COUNT(*) AS calls,
                       SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active_calls,
                       SUM(CASE WHEN benchmark_label='up' THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN benchmark_label='down' THEN 1 ELSE 0 END) AS stops,
                       SUM(CASE WHEN benchmark_label='timeout' THEN 1 ELSE 0 END) AS timeouts,
                       SUM(CASE WHEN status='abandoned' THEN 1 ELSE 0 END) AS abandons,
                       SUM(CASE WHEN benchmark_label IS NOT NULL THEN 1 ELSE 0 END)
                           AS benchmarked_calls,
                       AVG(CASE WHEN net_return_pct IS NOT NULL THEN net_return_pct END)
                           AS average_net_return_pct,
                       SUM(COALESCE(paper_pnl,0.0)) AS total_paper_pnl,
                       AVG(CASE WHEN benchmark_label IS NOT NULL
                                THEN (confidence-
                                      CASE WHEN benchmark_label='up' THEN 1.0 ELSE 0.0 END) *
                                     (confidence-
                                      CASE WHEN benchmark_label='up' THEN 1.0 ELSE 0.0 END)
                           END) AS brier_score
                FROM kol_calls
                GROUP BY predictor_id
            ), resolved AS (
                SELECT predictor_id,exit_at,id,
                       SUM(COALESCE(paper_pnl,0.0)) OVER (
                           PARTITION BY predictor_id ORDER BY exit_at,id
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS equity
                FROM kol_calls
                WHERE net_return_pct IS NOT NULL
            ), peaks AS (
                SELECT predictor_id,equity,
                       MAX(equity) OVER (
                           PARTITION BY predictor_id ORDER BY exit_at,id
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                       ) AS running_peak
                FROM resolved
            ), drawdowns AS (
                SELECT predictor_id,
                       MAX((CASE WHEN running_peak>0 THEN running_peak ELSE 0 END)-equity)
                           AS max_drawdown
                FROM peaks
                GROUP BY predictor_id
            )
            SELECT p.*,COALESCE(t.calls,0) AS score_calls,
                   COALESCE(t.active_calls,0) AS score_active_calls,
                   COALESCE(t.wins,0) AS score_wins,
                   COALESCE(t.stops,0) AS score_stops,
                   COALESCE(t.timeouts,0) AS score_timeouts,
                   COALESCE(t.abandons,0) AS score_abandons,
                   COALESCE(t.benchmarked_calls,0) AS score_benchmarked_calls,
                   t.average_net_return_pct,t.total_paper_pnl,t.brier_score,
                   COALESCE(d.max_drawdown,0.0) AS score_max_drawdown
            FROM kol_predictors p
            LEFT JOIN totals t ON t.predictor_id=p.id
            LEFT JOIN drawdowns d ON d.predictor_id=p.id
            """
            + ("" if include_hidden else "WHERE p.visible=1 ")
            + "ORDER BY CASE p.status WHEN 'champion' THEN 0 ELSE 1 END,p.created_at"
        ).fetchall()

    scorecards: list[dict[str, Any]] = []
    for raw in predictors:
        predictor = dict(raw)
        predictor["inference_model_label"] = model_display_name(
            str(predictor.get("inference_model") or "")
        )
        calls = int(predictor.pop("score_calls") or 0)
        active_calls = int(predictor.pop("score_active_calls") or 0)
        wins = int(predictor.pop("score_wins") or 0)
        stops = int(predictor.pop("score_stops") or 0)
        timeouts = int(predictor.pop("score_timeouts") or 0)
        abandons = int(predictor.pop("score_abandons") or 0)
        benchmarked_calls = int(predictor.pop("score_benchmarked_calls") or 0)
        average_return = predictor.pop("average_net_return_pct")
        paper_pnl = float(predictor.pop("total_paper_pnl") or 0.0)
        brier = predictor.pop("brier_score")
        max_drawdown = float(predictor.pop("score_max_drawdown") or 0.0)
        scorecards.append(
            {
                **predictor,
                "calls": calls,
                "active_calls": active_calls,
                "wins": wins,
                "stops": stops,
                "timeouts": timeouts,
                "abandons": abandons,
                "benchmarked_calls": benchmarked_calls,
                "hit_rate": round(wins / benchmarked_calls, 4) if benchmarked_calls else None,
                "average_net_return_pct": (
                    round(float(average_return), 4) if average_return is not None else None
                ),
                "paper_pnl": round(paper_pnl, 2),
                "max_drawdown": round(max_drawdown, 2),
                "brier_score": round(float(brier), 4) if brier is not None else None,
            }
        )
    return scorecards


def kol_status() -> dict[str, Any]:
    scorecards = predictor_scorecards()
    return {
        "predictors": scorecards,
        "calls": sum(int(row["calls"]) for row in scorecards),
        "active_calls": sum(int(row["active_calls"]) for row in scorecards),
        "paper_pnl_note": "$1,000 paper calls with estimated trading costs.",
        "research_policies": research_policy_scorecards(),
    }
