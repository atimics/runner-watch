"""Sports facts in the shared sentiment, attention, and risk visual grammar."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.attention import finite_number
from runner_web.stock_indicator import _indicator

POLICY = "sports-glyph-v1"
GROUPS = (("market", "Market activity", ("volume", "movement")), ("evidence", "News", ("news",)))
SOURCES = {"kalshi": "Kalshi", "polymarket": "Polymarket", "sportsbook": "Sportsbook"}


def _time(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _nonnegative(value: Any) -> float | None:
    number = finite_number(value)
    return number if number is not None and number >= 0 else None


def _attention(event: dict, selected: dict, reference: datetime) -> dict:
    """Bounded display index from activity, independent of model direction."""
    components, evidence, missing = {}, [], []
    volume_scores, movement_scores = [], []
    for source, label in SOURCES.items():
        points = sorted(
            (p for p in selected.get("points", []) if p.get("source") == source),
            key=lambda p: p["observed_at"],
        )[-160:]
        recent = [
            p
            for p in points
            if (at := _time(p.get("observed_at"))) is not None
            and reference - timedelta(hours=24) <= at <= reference
        ]
        if not recent or reference - _time(recent[-1]["observed_at"]) > timedelta(minutes=30):
            continue
        latest = recent[-1]
        volume = _nonnegative(latest.get("volume_24h"))
        unit = latest.get("volume_unit")
        if volume is not None and unit in {"contracts", "USD"}:
            volume_scores.append(min(50, 10 * math.log10(1 + volume)))
            evidence.append(
                f"{label}: {volume:,.0f} {unit} traded in 24h "
                f"({latest.get('volume_scope') or 'market'})"
            )
        # Price range describes observed activity; repeated collection has no weight.
        usable = [p for p in recent if p.get("quality", "quoted") == "quoted"]
        if (
            latest.get("quality", "quoted") == "quoted"
            and len(usable) >= 2
            and _time(usable[-1]["observed_at"]) - _time(usable[0]["observed_at"])
            >= timedelta(minutes=10)
        ):
            prices = [p["probability"] for p in usable]
            move = (max(prices) - min(prices)) * 100
            movement_scores.append(min(30, 3 * move))
            evidence.append(f"{label}: {move:.1f} pp price range in saved readings within 24h")
    if volume_scores:
        components["volume"] = max(volume_scores)
    else:
        missing.append("Fresh 24h trading volume")
    if movement_scores:
        components["movement"] = max(movement_scores)
    else:
        missing.append("Fresh price history spanning at least 10 minutes")
    articles = event.get("news") or []
    known = [
        a
        for a in articles
        if (at := _time(a.get("published_at"))) is not None
        and at <= reference
        and (collected := _time(a.get("collected_at"))) is not None
        and collected <= reference
    ]
    if known:
        unique = {
            a.get("source_url") or a.get("id")
            for a in known
            if _time(a["published_at"]) >= reference - timedelta(hours=24)
            and (a.get("source_url") or a.get("id"))
        }
        components["news"] = min(20, 4 * len(unique))
        evidence.append(f"{len(unique)} linked news stories published in 24h")
    else:
        missing.append("Linked news coverage")
    return {
        "score": round(sum(components.values()), 1) if components else None,
        "components": components,
        "evidence": evidence,
        "missing": missing,
    }


def sports_indicator(event: dict, ticker: dict, reference: datetime, current: datetime) -> dict:
    selected = ticker.get("selected") or {}
    contract = ticker.get("contract") or {}
    benchmark = selected.get("benchmark") or {}
    gap = selected.get("gap")
    side = selected.get("label", "Selected outcome")
    tone = (
        "unknown"
        if gap is None
        else "positive"
        if gap > 0
        else "negative"
        if gap < 0
        else "neutral"
    )
    sentiment = (
        f"{side}: RATi {abs(gap):.1f} pp {'above' if gap > 0 else 'below'} {benchmark['label']}"
        if gap
        else f"{side}: RATi in line with {benchmark['label']}"
        if gap == 0
        else f"{side}: sentiment pending a fresh model and comparable market price"
    )
    activity = _attention(event, selected, reference)
    factors, significant = [], []
    if selected.get("model") is not None:
        factors.append(contract.get("model_note") or "Experimental sports baseline")
    else:
        factors.append("Model coverage pending")
    model_at = _time(selected.get("model_at"))
    if selected.get("model") is not None and (
        model_at is None
        or not reference - timedelta(minutes=30) <= model_at <= reference
        or contract.get("stale")
    ):
        significant.append("Model refresh needed for this comparison")
    venues = selected.get("venues") or []
    if not venues:
        factors.append("Comparable market coverage pending")
    for venue in venues:
        label = venue["label"]
        if venue.get("stale"):
            significant.append(f"{label}: price refresh needed")
        quality = venue.get("quality", "quoted")
        spread = finite_number(venue.get("spread"))
        if quality == "wide spread":
            significant.append(f"{label}: wide bid/ask spread")
        elif quality != "quoted":
            factors.append(f"{label}: price quality check pending")
        elif spread is not None and spread >= 0.05:
            factors.append(f"{label}: {spread * 100:.1f} pp bid/ask spread")
    fresh = [v["probability"] for v in venues if v.get("gap") is not None]
    if len(fresh) >= 2 and max(fresh) - min(fresh) >= 0.10:
        factors.append(f"Venues differ by {(max(fresh) - min(fresh)) * 100:.1f} pp")
    factors.extend(
        reason
        for reason in (event.get("prediction") or {}).get("risks", [])
        if isinstance(reason, str) and reason.strip()
    )
    factors = list(dict.fromkeys(significant + factors))
    glyph = _indicator(
        {
            "attention_score": activity["score"],
            "score_components": activity["components"],
            "risks": factors,
            "significant_risk_factor_count": len(significant),
        },
        GROUPS,
        tone,
        "RATi sentiment",
    )
    # A direction ring represents one selected outcome, with the gap stated in pp.
    glyph["sentiment_mix"].update(
        description=sentiment,
        compact=sentiment,
        basis="RATi minus the named market for this outcome",
    )
    if gap == 0:
        glyph["sentiment_mix"].update(
            state="available",
            bullish=0,
            bearish=0,
            gradient="conic-gradient(var(--muted) 0% 100%)",
        )
    if not selected:
        glyph["risk"] = "unknown"
    risk_reading = (
        "Risk checks pending"
        if glyph["risk"] == "unknown"
        else "Comparison needs a closer check"
        if significant
        else "Model and market limits apply"
    )
    glyph["risk_reading"] = risk_reading
    scope = "Saved pregame reading" if reference < current else "Current reading"
    attention_text = (
        f"Attention {activity['score']:g}/100 · {glyph['band_label'].lower()}"
        if activity["score"] is not None
        else "Attention pending activity data"
    )
    glyph.update(
        policy=POLICY,
        attention_reading=attention_text,
        attention_evidence=activity["evidence"],
        attention_missing=activity["missing"],
        scope=scope,
        as_of=reference.strftime("%b %d · %H:%M UTC"),
        description=f"{sentiment}. {attention_text}. Risk: {risk_reading.lower()}. {scope}.",
    )
    return glyph
