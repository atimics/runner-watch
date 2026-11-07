"""Stock scoring from saved inputs and explicit observation clocks."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web import attention
from runner_web.market_marks import usable_market_mark as _usable_market_mark
from runner_web.product_policy import EVIDENCE_GATE

PUBLIC_SCORE_DRIVERS = (
    ("market", "Scan"),
    ("sec_event", "SEC"),
    ("news", "News"),
    ("social_search", "Social"),
    ("cluster", "Cluster holdings"),
    ("community", "Community"),
)

PUBLIC_SCORE_PENALTIES = (
    ("safety", "Safety"),
    ("rug", "Rug"),
    ("state", "State"),
)


def _timestamp(value: Any) -> datetime | None:
    try:
        observed_at = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return observed_at.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


FILING_SENTIMENT_BASIS = (
    "Filings collected in the past 3 days, by form type. Offerings, late reports, "
    "proposed sales and insider sales are flagged bearish; no form type is scored "
    "bullish, so this counts risk flags rather than measuring a balance"
)


def evidence_gate(
    current: dict[str, Any],
    events: list[dict[str, Any]],
    pressure: dict[str, Any] | None = None,
    external_context: dict[str, Any] | None = None,
    base_rates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_checks: list[str] = []
    blockers: list[str] = []
    relative_volume = _number(current.get("relative_volume"))
    recent_relative_volume = _number(current.get("recent_relative_volume"))
    momentum_15m = _number(current.get("momentum_15m_pct"))
    acceleration = _number(current.get("momentum_acceleration_pct"))
    vwap_position = _number(current.get("vwap_position_pct"))
    breakout = _number(current.get("breakout_pct"))
    if relative_volume is not None and relative_volume >= 2:
        market_checks.append("Unusual volume")
    if recent_relative_volume is not None and recent_relative_volume >= 3:
        market_checks.append("Volume burst")
    if momentum_15m is not None and momentum_15m >= 3:
        market_checks.append("15m momentum")
    if acceleration is not None and acceleration >= 0.5:
        market_checks.append("Momentum rising")
    if vwap_position is not None and vwap_position > 0:
        market_checks.append("Above VWAP")
    if breakout is not None and breakout > 0:
        market_checks.append("Above prior high")
    if pressure and pressure.get("available") and pressure.get("buy_pressure_pct", 0) >= 60:
        market_checks.append("Buy pressure")

    positive_primary = current.get("catalyst_sentiment") == "positive" or any(
        event.get("sentiment") == "positive" for event in events
    )
    context = external_context or {}
    news_count = int(context.get("news_count") or current.get("news_count") or 0)
    social_mentions = int(
        context.get("social_mentions") or current.get("external_social_mentions") or 0
    )
    call_count = int(current.get("call_count") or 0)
    comment_count = int(current.get("comment_count") or 0)
    community_count = call_count + comment_count

    baseline_mode = str((base_rates or {}).get("mode") or "unavailable")
    notable_metrics = list((base_rates or {}).get("notable_metrics") or [])
    market_confirmed = bool(market_checks) and (
        baseline_mode != "empirical" or bool(notable_metrics)
    )
    crowd_confirmed = social_mentions > 0 or community_count >= 2
    family_receipts = [
        {
            "family": "market",
            "label": "Market structure",
            "status": "confirmed" if market_confirmed else "not_confirmed",
            "mode": "derived",
            "source": "market bars",
            "evidence": market_checks,
            "base_rate": base_rates,
        },
        {
            "family": "primary",
            "label": "Primary filing",
            "status": "confirmed" if positive_primary else "not_confirmed",
            "mode": "observed",
            "source": "SEC",
            "evidence": ["Positive SEC filing"] if positive_primary else [],
        },
        {
            "family": "news",
            "label": "News coverage",
            "status": "confirmed" if news_count > 0 else "not_confirmed",
            "mode": "observed",
            "source": "news metadata",
            "evidence": [f"{news_count} fresh article{'s' if news_count != 1 else ''}"]
            if news_count > 0
            else [],
        },
        {
            "family": "crowd",
            "label": "Crowd activity",
            "status": "confirmed" if crowd_confirmed else "not_confirmed",
            "mode": "observed",
            "source": "public social and community",
            "evidence": [
                *(
                    [f"{social_mentions} public mention{'s' if social_mentions != 1 else ''}"]
                    if social_mentions > 0
                    else []
                ),
                *(
                    [f"{call_count} public Calls and {comment_count} comments"]
                    if community_count >= 2
                    else []
                ),
            ],
        },
    ]
    confirmed_families = [
        receipt for receipt in family_receipts if receipt["status"] == "confirmed"
    ]
    checks = [str(receipt["label"]) for receipt in confirmed_families]
    rug_score = _number(current.get("rug_score"))
    rug_level = str(current.get("rug_level") or "UNKNOWN").upper()
    raw_trade_state = current.get("trade_state")
    trade_state = str(raw_trade_state).upper() if raw_trade_state else "UNKNOWN"
    if bool(current.get("hard_veto")):
        blockers.append("Blocked by risk rule")
    if rug_score is not None and rug_score >= 75:
        blockers.append("Risk factors triggered a block")
    if trade_state in {"AVOID", "EXIT"}:
        blockers.append(f"State: {trade_state.title()}")
    eligibility_state = (current.get("eligibility") or {}).get("state")
    if eligibility_state == "blocked" and not blockers:
        blockers.append("Blocked by current eligibility")
    threshold = EVIDENCE_GATE.threshold
    evidence_count = len(confirmed_families)
    if blockers:
        state = "blocked"
    elif eligibility_state == "unknown":
        state = "gathering"
    elif (
        market_confirmed
        and evidence_count >= threshold
        and trade_state
        in {
            "TRIGGERED",
            "MANAGE",
            "UNKNOWN",
        }
    ):
        state = "ready"
    elif (
        market_confirmed
        and evidence_count >= threshold - 1
        and trade_state
        in {
            "ARMED",
            "TRIGGERED",
            "MANAGE",
            "UNKNOWN",
        }
    ):
        state = "near"
    else:
        state = "gathering"
    return {
        "count": min(evidence_count, threshold),
        "threshold": threshold,
        "state": state,
        "checks": checks,
        "blockers": blockers,
        "rug_score": rug_score,
        "rug_level": rug_level,
        "trade_state": trade_state,
        "families": family_receipts,
        "raw_market_checks": market_checks,
        "required_family": "market",
        "base_rates": base_rates,
        "baseline_summary": baseline_summary(base_rates),
        "summary": (
            "Blocked by risk"
            if state == "blocked"
            else "Confirmed"
            if state == "ready"
            else "Almost ready"
            if state == "near"
            else "Waiting for evidence"
        ),
    }


def baseline_summary(base_rates: dict[str, Any] | None) -> str | None:
    if not base_rates or str(base_rates.get("mode") or "") == "deferred":
        return None
    metrics = base_rates.get("metrics") or {}
    empirical = [
        receipt
        for receipt in metrics.values()
        if isinstance(receipt, dict) and receipt.get("mode") == "empirical"
    ]
    if not empirical:
        matched = int(base_rates.get("matched_sessions") or 0)
        minimum = int(base_rates.get("minimum_samples") or 0)
        return f"Baseline learning: {matched}/{minimum} matched sessions"
    notable = [receipt for receipt in empirical if receipt.get("notable")]
    if not notable:
        return "Market readings are normal for matched sessions"
    strongest = max(notable, key=lambda receipt: float(receipt.get("percentile") or 0))
    percentile = round(float(strongest["percentile"]) * 100)
    return (
        f"{strongest['label']} is above {percentile}% of "
        f"{strongest['sample_count']} matched sessions"
    )


def directional_thesis(prediction: dict[str, Any] | None) -> dict[str, Any] | None:

    if not prediction:
        return None
    probabilities: dict[str, float] = {}
    for outcome in ("up", "down", "timeout"):
        value = prediction.get(f"probability_{outcome}")
        try:
            probability = float(value)
        except (TypeError, ValueError):
            return None
        if not 0 <= probability <= 1:
            return None
        probabilities[outcome] = probability

    total_probability = sum(probabilities.values())
    if not 0.98 <= total_probability <= 1.02:
        return None
    probabilities = {
        outcome: probability / total_probability for outcome, probability in probabilities.items()
    }

    try:
        expected_return_pct = float(prediction.get("expected_return_pct"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(expected_return_pct):
        return None

    ranked_outcome = "up" if expected_return_pct > 0 else "down"
    directional_margin = probabilities[ranked_outcome] - probabilities["timeout"]
    if expected_return_pct == 0 or directional_margin < 0.05:
        outcome = "timeout"
    else:
        outcome = ranked_outcome

    ordered_outcomes = ("down", "timeout", "up")
    raw_percentages = [probabilities[key] * 100 for key in ordered_outcomes]
    display_percentages = [math.floor(value) for value in raw_percentages]
    remaining = 100 - sum(display_percentages)
    fractions = sorted(
        range(len(ordered_outcomes)),
        key=lambda index: (raw_percentages[index] - display_percentages[index], -index),
        reverse=True,
    )
    for index in fractions[:remaining]:
        display_percentages[index] += 1

    outcome_copy = {
        "down": ("−4% first", "Down barrier"),
        "timeout": ("No barrier", "Neither barrier"),
        "up": ("+8% first", "Up barrier"),
    }
    distribution = [
        {
            "key": key,
            "label": outcome_copy[key][0],
            "accessible_label": outcome_copy[key][1],
            "probability": round(probabilities[key], 6),
            "probability_pct": display_percentages[index],
        }
        for index, key in enumerate(ordered_outcomes)
    ]

    direction = "flat" if outcome == "timeout" else outcome
    labels = {
        "up": "Upside setup",
        "down": "Downside pressure",
        "timeout": "No directional call",
    }
    arrows = {"up": "↑", "down": "↓", "timeout": "↔"}
    model_status = str(prediction.get("model_status") or "shadow").lower()
    status_label = "Live model" if model_status == "active" else "Shadow model"
    evidence_at = str(prediction.get("created_at") or "") or None
    model_id = str(prediction.get("model_id") or "unversioned")
    accessible_distribution = "; ".join(
        f"{item['probability_pct']} percent {item['label']}" for item in distribution
    )
    return {
        "direction": direction,
        "label": labels[outcome],
        "arrow": arrows[outcome],
        "probability": round(probabilities[outcome], 4),
        "probability_pct": display_percentages[ordered_outcomes.index(outcome)],
        "probability_up": round(probabilities["up"], 6),
        "probability_down": round(probabilities["down"], 6),
        "probability_timeout": round(probabilities["timeout"], 6),
        "distribution": distribution,
        "expected_return_pct": round(expected_return_pct, 4),
        "horizon": "60m",
        "horizon_label": "60 minutes",
        "contract": "+8% before −4% within 60 minutes",
        "model_id": model_id,
        "model_status": model_status,
        "status_label": status_label,
        "evidence_at": evidence_at,
        "accessible_description": (
            f"Directional thesis: {labels[outcome]}, 60 minutes. "
            f"{accessible_distribution}. {status_label}, model {model_id}."
        ),
    }


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("payload_json")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nonnegative_event_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _event_timestamp(row: dict[str, Any]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(row.get("event_at") or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def external_event_context(rows: list[dict[str, Any]], *, at: datetime) -> dict[str, Any]:
    from runner_web.nasdaq_halts import latest_halt_versions

    checked_at = at
    news: list[dict[str, Any]] = []
    social_by_source: dict[str, dict[str, Any]] = {}
    news_keys: set[str] = set()
    active_halt: dict[str, Any] | None = None
    seen_halts: set[str] = set()
    for row in latest_halt_versions(rows):
        event = {**row, "payload": _event_payload(row)}
        timestamp = _event_timestamp(event)
        if timestamp is None or timestamp > checked_at:
            continue
        if event.get("event_type") == "news_article":
            if timestamp and timestamp >= checked_at - timedelta(hours=24):
                key = str(
                    event.get("source_url")
                    or event["payload"].get("url")
                    or (
                        f"{event.get('source')}:{event.get('event_at')}:"
                        f"{event['payload'].get('title')}"
                    )
                )
                if key not in news_keys:
                    news_keys.add(key)
                    news.append(event)
        elif event.get("event_type") == "social_spike":
            if timestamp and timestamp >= checked_at - timedelta(hours=6):
                source = str(event.get("source") or "unknown")
                current = social_by_source.get(source)
                if current is None or str(event.get("event_at") or "") > str(
                    current.get("event_at") or ""
                ):
                    social_by_source[source] = event
        elif event.get("event_type") == "trading_halt":
            ticker = str(event.get("ticker") or "")
            if ticker in seen_halts:
                continue
            seen_halts.add(ticker)
            status = str(event.get("status") or "").lower()
            resume_at = _event_timestamp({"event_at": event["payload"].get("trade_resume_at")})
            if (resume_at is not None and resume_at > checked_at) or (
                resume_at is None and status in {"active", "halted", "pending"}
            ):
                active_halt = active_halt or event

    news.sort(key=lambda event: str(event.get("event_at") or ""), reverse=True)
    social = list(social_by_source.values())
    social.sort(key=lambda event: str(event.get("event_at") or ""), reverse=True)
    mention_count = sum(
        _nonnegative_event_count(event["payload"].get("mention_count")) for event in social
    )
    engagement_count = sum(
        _nonnegative_event_count(event["payload"].get("engagement_count")) for event in social
    )
    news_boost = min(6.0, 1.5 * math.sqrt(len(news)))
    social_boost = min(
        8.0,
        math.log2(mention_count + 1) + 0.5 * math.log2(engagement_count + 1),
    )
    return {
        "news_count": len(news),
        "news_boost": round(news_boost, 2),
        "latest_news": news[0] if news else None,
        "social_mentions": mention_count,
        "social_engagement": engagement_count,
        "social_search_boost": round(social_boost, 2),
        "latest_social": social[0] if social else None,
        "active_halt": active_halt,
        "safety_penalty": 25.0 if active_halt else 0.0,
        "normalized_event_count": len(rows),
    }


def external_event_label(context: dict[str, Any]) -> tuple[str, str, str | None] | None:
    halt = context.get("active_halt")
    if halt:
        return (
            f"Trading halt · {halt.get('status') or 'active'}",
            str(halt.get("source") or "nasdaq_trader"),
            halt.get("source_url"),
        )
    latest_news = context.get("latest_news")
    latest_social = context.get("latest_social")
    candidates = [event for event in (latest_news, latest_social) if event]
    if not candidates:
        return None
    latest = max(candidates, key=lambda event: str(event.get("event_at") or ""))
    if latest.get("event_type") == "news_article":
        title = str(latest.get("payload", {}).get("title") or "New company coverage")
        return (
            f"News · {title[:96]}",
            str(latest.get("source") or "news"),
            latest.get("source_url"),
        )
    mentions = int(context.get("social_mentions") or 0)
    network = str(latest.get("payload", {}).get("network_label") or "Social")
    noun = "cashtag mention" if network == "Bluesky" else "mention"
    return (
        f"{network} · {mentions} {noun}{'s' if mentions != 1 else ''}",
        str(latest.get("source") or "social"),
        latest.get("source_url"),
    )


def score_snapshot(
    snapshot: dict[str, Any], inputs: dict[str, Any], *, at: datetime, include_trace: bool = False
) -> dict[str, Any]:
    ticker = snapshot["ticker"]
    catalyst = inputs["filings_by_ticker"].get(ticker)
    prediction = inputs["predictions"].get(str(snapshot["id"]))
    facts = attention.forecast_facts(prediction)
    computed_at = _timestamp(inputs["score_as_of"])
    quote_at = _timestamp(snapshot.get("quote_time"))
    quote_mark = (
        _usable_market_mark(
            snapshot,
            inputs.get("quote_marks", {}).get(ticker),
            computed_at,
        )
        if computed_at is not None
        else None
    )
    eligibility_quote_at = quote_mark[2] if quote_mark else quote_at
    age_minutes = (
        (computed_at - quote_at).total_seconds() / 60
        if computed_at is not None and quote_at is not None
        else None
    )
    eligibility_age_minutes = (
        (computed_at - eligibility_quote_at).total_seconds() / 60
        if computed_at is not None and eligibility_quote_at is not None
        else None
    )
    captured_at = _timestamp(snapshot.get("captured_at"))
    assessment_age_minutes = (
        (computed_at - captured_at).total_seconds() / 60
        if computed_at is not None and captured_at is not None
        else None
    )
    activity = attention.market_activity({**snapshot, "stale_minutes": age_minutes})
    custom_score = float(activity["value"])
    catalyst_score = (attention.finite_number(catalyst.get("score")) or 0.0) if catalyst else 0.0
    catalyst_sentiment = str(catalyst.get("sentiment") or "") if catalyst else ""
    # A filing's size moves attention whatever its direction; whether it is
    # bearish belongs to the forecast and the block, not to notice.
    event_boost = attention.event_attention(catalyst_score)
    community_counts = inputs["community"].get(ticker, {"call_count": 0, "comment_count": 0})
    call_count = community_counts["call_count"]
    comment_count = community_counts["comment_count"]
    # Calls and comments remain descriptive; neither rewards the board for exposure.
    engagement_count = call_count
    community_boost = attention.community_attention(call_count)
    external = external_event_context(
        inputs["market_events_by_ticker"].get(ticker, []),
        at=_timestamp(inputs["score_as_of"]) or at,
    )
    news_boost = float(external["news_boost"])
    social_search_boost = float(external["social_search_boost"])
    cluster_summary = inputs.get("cluster_summaries", {}).get(ticker) or {}
    cluster_value = cluster_summary.get("value")
    stock_holding = next(
        (
            stock.get("value")
            for stock in cluster_summary.get("stocks", [])
            if stock["ticker"] == ticker
        ),
        None,
    )
    cluster_boost = attention.cluster_attention(cluster_value, stock_holding)
    safety_penalty = float(external["safety_penalty"])
    raw_rug_score = snapshot.get("rug_score")
    rug_score = attention.finite_number(raw_rug_score)
    trade_state = str(snapshot.get("trade_state") or "UNKNOWN").upper()
    if external.get("active_halt"):
        rug_score = max(rug_score or 0.0, 90.0)
        trade_state = "AVOID"
    rug_penalty = (rug_score or 0.0) * 0.30
    state_penalty = 25.0 if trade_state == "EXIT" else 20.0 if trade_state == "AVOID" else 0.0
    # Attention is what deserves a look; the deductions below are policy, and
    # they are reported through eligibility instead of shrinking the number.
    pulse_score = attention.attention_score(
        signal=custom_score,
        event=event_boost,
        news=news_boost,
        social=social_search_boost,
        cluster=cluster_boost,
        community=community_boost,
    )
    can_act = attention.eligibility(
        active_halt=bool(external.get("active_halt")),
        trade_state=trade_state,
        rug_score=rug_score,
        rug_level=str(snapshot.get("rug_level") or ""),
        has_price=(
            quote_mark[0] if quote_mark else (attention.finite_number(snapshot.get("price")) or 0)
        )
        > 0,
        hard_veto=bool(snapshot.get("hard_veto")),
        stale_minutes=eligibility_age_minutes,
        assessment_age_minutes=assessment_age_minutes,
        require_complete=True,
    )
    # Only what moved attention lives here. The deductions are policy, and they
    # are reported beside the score rather than mixed into it.
    score_components = {
        "market": round(custom_score, 2),
        "sec_event": round(event_boost, 2),
        "news": news_boost,
        "social_search": social_search_boost,
        "cluster": round(cluster_boost, 2),
        "community": round(community_boost, 2),
    }
    policy_components = {
        "safety": -safety_penalty,
        "rug": -round(rug_penalty, 2),
        "state": -state_penalty,
    }
    score_trace = {}
    if include_trace:

        def trace(*rows: tuple[str, Any]) -> list[dict[str, str]]:
            return [{"label": label, "value": str(value)} for label, value in rows]

        score_trace = {
            "market": trace(
                ("Source", "Direction-neutral activity heuristic; not a probability"),
                ("Input score", f"{custom_score:g}"),
                ("Calculation", "Capped volume, absolute momentum and absolute move × freshness"),
            ),
            "sec_event": trace(
                ("Filing", catalyst.get("form") or "SEC filing" if catalyst else "SEC filing"),
                ("Sentiment", catalyst_sentiment or "Neutral"),
                ("Filing score", f"{catalyst_score:g}"),
                (
                    "Calculation",
                    "12% of filing score, capped at +12 points"
                    if catalyst
                    else "No filing: 0 points",
                ),
            ),
            "news": trace(
                ("Articles in 24 hours", external["news_count"]),
                ("Calculation", "1.5 × √articles, capped at +6 points"),
            ),
            "social_search": trace(
                ("Mentions in 6 hours", external["social_mentions"]),
                ("Engagements in 6 hours", external["social_engagement"]),
                (
                    "Calculation",
                    "log₂(mentions + 1) + 0.5 × log₂(engagements + 1), capped at +8 points",
                ),
            ),
            "cluster": trace(
                (
                    "Tracked cluster holdings",
                    f"${cluster_value:,.0f}" if cluster_value is not None else "Unavailable",
                ),
                (
                    "Holding in this stock",
                    f"${stock_holding:,.0f}" if stock_holding is not None else "Unavailable",
                ),
                (
                    "Calculation",
                    "2 × log₁₀(1 + cluster holdings / $10,000) × √(stock share), "
                    "capped at +8 points",
                ),
            ),
            "community": trace(
                ("Callers with active Calls", call_count),
                ("Public comments", comment_count),
                ("Calculation", "Engagement is descriptive only: 0 attention points"),
            ),
            "safety": trace(
                ("Trading halt", "Active" if external.get("active_halt") else "Clear"),
                ("Calculation", "Reported through eligibility; attention is not reduced"),
            ),
            "rug": trace(
                ("Rug score", f"{rug_score:g}" if rug_score is not None else "0"),
                ("Calculation", "Reported through eligibility; attention is not reduced"),
            ),
            "state": trace(
                ("Trade state", trade_state),
                ("Calculation", "Reported through eligibility; attention is not reduced"),
            ),
        }
    return {
        "baseline_score": attention.finite_number(snapshot.get("score")),
        "sentiment_counts": inputs.get("sentiment_counts", {}).get(
            ticker, {"bullish": 0, "bearish": 0}
        ),
        "sentiment_basis": FILING_SENTIMENT_BASIS,
        "rug_score": rug_score,
        "trade_state": trade_state,
        "model_score": 100 * facts["probability_up"] if facts else None,
        "score_policy": attention.POLICY_VERSION,
        "score_unit": "heuristic_points",
        "attention_basis": "direction_neutral_activity",
        "attention_urgent": bool(external.get("active_halt")),
        "attention_urgency_reason": "Active trading halt" if external.get("active_halt") else None,
        "activity_inputs": activity,
        "forecast": facts,
        "feature_as_of": snapshot.get("captured_at"),
        "activity_quote_as_of": snapshot.get("quote_time"),
        "quote_as_of": eligibility_quote_at.isoformat() if eligibility_quote_at else None,
        "computed_at": inputs["score_as_of"],
        "model_rank": prediction.get("rank") if prediction else None,
        "score": pulse_score,
        "custom_score": pulse_score,
        "attention_score": pulse_score,
        "eligibility": can_act,
        "eligibility_note": attention.risk_note(can_act),
        "policy_components": policy_components,
        "score_as_of": inputs["score_as_of"],
        "score_snapshot_id": snapshot["id"],
        "runner_probability": facts["probability_up"] if facts else None,
        "runner_probability_down": facts["probability_down"] if facts else None,
        "runner_probability_timeout": facts["probability_timeout"] if facts else None,
        "directional_thesis": directional_thesis(prediction) if facts else None,
        "expected_return_pct": facts["assumed_barrier_payoff_pct"] if facts else None,
        "call_count": call_count,
        "comment_count": comment_count,
        "engagement_count": engagement_count,
        "event_boost": round(event_boost, 2),
        "news_boost": news_boost,
        "social_search_boost": social_search_boost,
        "cluster_boost": round(cluster_boost, 2),
        "community_boost": round(community_boost, 2),
        "safety_penalty": safety_penalty,
        "rug_penalty": round(rug_penalty, 2),
        "state_penalty": state_penalty,
        "active_market_event": external.get("active_halt"),
        "news_count": external["news_count"],
        "external_social_mentions": external["social_mentions"],
        "external_social_engagement": external["social_engagement"],
        "latest_news": external.get("latest_news"),
        "score_components": score_components,
        "score_trace": score_trace,
        "score_detail": public_score_detail(score_components, pulse_score),
        "external_context": external,
    }


def public_score_detail(components: dict[str, Any], score: float) -> dict[str, Any]:
    """Keep the public score breakdown small enough to ship with every row."""

    return {
        "score": round(float(score), 1),
        "drivers": [
            {"key": key, "label": label, "value": round(float(components.get(key) or 0.0), 1)}
            for key, label in PUBLIC_SCORE_DRIVERS
        ],
        "penalties": [
            {"key": key, "label": label, "value": round(float(components.get(key) or 0.0), 1)}
            for key, label in PUBLIC_SCORE_PENALTIES
            if float(components.get(key) or 0.0) != 0.0
        ],
    }
