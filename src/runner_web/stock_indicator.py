"""Shared presentation for stock and token attention glyphs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runner_web.attention import finite_number

GROUPS = (
    ("market", "Market", ("market",)),
    ("evidence", "Filings + news", ("sec_event", "news")),
    ("social", "External social", ("social_search",)),
)
TOKEN_GROUPS = (
    ("market", "Market", ("market",)),
    ("evidence", "Chain evidence", ("chain_event",)),
    ("social", "External social", ("social_search",)),
)
RISK_READINGS = {
    "significant": "1+ significant risk factors detected in saved checks",
    "detected": "Risk factors detected in saved checks",
    "none": "Detected risk factors: 0 in saved checks",
    "unknown": "Risk factor checks unavailable",
}
VERIFICATION_NOTE = (
    "Verified evidence — automated: the existing evidence gate is confirmed and "
    "eligibility checks pass. Not human review, identity verification or an investment endorsement."
)


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _risk(item: Mapping) -> str:
    # Describe detected factors separately from internal policy grades.
    significant = finite_number(item.get("significant_risk_factor_count"))
    if (
        item.get("hard_veto")
        or item.get("attention_urgent")
        or (significant is not None and significant >= 1 and significant.is_integer())
    ):
        return "significant"
    reasons = item.get("risks")
    if isinstance(reasons, list) and any(isinstance(r, str) and r.strip() for r in reasons):
        return "detected"
    level = str(item.get("rug_level") or "").lower()
    if level in {"guarded", "medium", "high", "critical"}:
        return "detected"
    score = finite_number(item.get("rug_score"))
    if score is not None and 0 <= score <= 100:
        return "detected" if score > 0 else "none"
    return "unknown"


def _verification(item: Mapping) -> dict[str, Any]:
    gate = _mapping(item.get("evidence_gate"))
    eligibility = _mapping(item.get("eligibility"))
    verified = (
        gate.get("state") == "ready"
        and eligibility.get("state") == "eligible"
        and not gate.get("blockers")
        and not item.get("hard_veto")
    )
    return {
        "verified": verified,
        "basis": "automated_evidence_gate" if verified else None,
        "label": "Verified evidence — automated" if verified else "Not verified",
        "note": VERIFICATION_NOTE,
        "as_of": item.get("computed_at") or item.get("score_as_of"),
    }


def stock_indicator(item: Mapping[str, Any]) -> dict[str, Any]:
    glyph = _indicator(
        item,
        GROUPS,
        item.get("sentiment") or item.get("catalyst_sentiment"),
        "Filing sentiment",
        item.get("sentiment_counts"),
        item.get("sentiment_basis"),
    )
    glyph["verification"] = _verification(item)
    return glyph


def memecoin_indicator(item: Mapping[str, Any]) -> dict[str, Any]:
    """Render saved assessments; quote and receipt counts alone leave values unknown."""
    return _indicator(
        item,
        TOKEN_GROUPS,
        item.get("chain_sentiment"),
        "Chain evidence tone",
        item.get("chain_sentiment_counts"),
        item.get("chain_sentiment_basis"),
    )


def _sentiment_mix(tone: Any, counts: Any, label: str, basis: Any) -> dict[str, Any]:
    """Share of saved directional readings. Neutral-only evidence keeps a gap."""
    if counts is None:
        direction = str(tone or "").lower()
        bullish = float(direction in {"positive", "bullish"})
        bearish = float(direction in {"negative", "risk", "bearish"})
    else:
        values = _mapping(counts)
        bullish = finite_number(values.get("bullish"))
        bearish = finite_number(values.get("bearish"))
    valid = bullish is not None and bearish is not None and bullish >= 0 and bearish >= 0
    total = bullish + bearish if valid else 0
    available = valid and 0 < total < float("inf")
    share = bullish / total if available else None
    bullish_percent = int(share * 100 + 0.5) if available else None
    description = (
        f"{label}: {int(share * 100 + 0.5)}% bullish, {100 - int(share * 100 + 0.5)}% bearish."
        if available
        else f"{label}: bullish/bearish split unavailable."
    )
    return {
        "state": "available" if available else "unknown",
        "bullish": share,
        "bearish": 1 - share if available else None,
        "compact": (f"▲{bullish_percent}% / ▼{100 - bullish_percent}%" if available else "▲— / ▼—"),
        "description": description,
        "basis": str(basis or f"Saved {label.lower()} assessments"),
        "gradient": (
            f"conic-gradient(var(--green) 0% {share * 100:.6f}%, "
            f"var(--red) {share * 100:.6f}% 100%)"
            if available
            else ""
        ),
    }


def _indicator(
    item: Mapping,
    groups: tuple,
    tone: Any,
    tone_label: str,
    sentiment_counts: Any = None,
    sentiment_basis: Any = None,
) -> dict[str, Any]:
    """One scale for attention; proportional contributions, not mixed score units.

    Values come from post-freshness score contributions before the final cap.
    Legacy negative penalties and internal community activity never form slices.
    Missing breakdowns stay missing; a nonzero score does not invent a blue slice.
    """
    score = finite_number(item.get("attention_score", item.get("score")))
    if score is not None:
        score = min(100.0, max(0.0, score))
    band = 3 if score is not None and score >= 70 else 2 if score is not None and score >= 40 else 1
    components = item.get("score_components")
    if not isinstance(components, Mapping):
        drivers = _mapping(item.get("score_detail")).get("drivers")
        components = (
            {
                part.get("key"): part.get("value")
                for part in drivers or []
                if isinstance(part, Mapping)
            }
            if isinstance(drivers, list)
            else {}
        )
    slices = []
    for key, label, fields in groups:
        value = sum(max(0.0, finite_number(components.get(field)) or 0.0) for field in fields)
        slices.append({"key": key, "label": label, "value": value})
    total = sum(part["value"] for part in slices)
    # Reject a corrupt breakdown if otherwise finite inputs overflow when summed.
    if total == float("inf"):
        total = 0.0
        for part in slices:
            part["value"] = 0.0
    cursor = 0.0
    stops = []
    for part in slices:
        share = part["value"] / total if total > 0 else 0.0
        end = cursor + share * 100.0
        part.update(share=share, start=round(cursor, 6), end=round(end, 6))
        if share:
            stops.append(f"var(--indicator-{part['key']}) {cursor:.6f}% {end:.6f}%")
        cursor = end
    supplied = any(
        finite_number(components.get(field)) is not None
        for _, _, fields in groups
        for field in fields
    )
    mix_state = "available" if total > 0 else "zero" if supplied and score == 0 else "unknown"
    sentiment = {
        "positive": "positive",
        "bullish": "positive",
        "risk": "negative",
        "negative": "negative",
        "bearish": "negative",
        "neutral": "neutral",
        "mixed": "neutral",
    }.get(str(tone or "").lower(), "unknown")
    sentiment_mix = _sentiment_mix(tone, sentiment_counts, tone_label, sentiment_basis)
    risk = _risk(item)
    attention_text = f"Attention {score:g} points" if score is not None else "Attention unavailable"
    mix_text = (
        "; ".join(
            f"{part['label']} {part['share']:.0%} of contributions ({part['value']:g} points)"
            for part in slices
            if part["value"] > 0
        )
        if total > 0
        else "No attention contributions"
        if mix_state == "zero"
        else "Attention breakdown unavailable"
    )
    return {
        "score": score,
        "band": band,
        "band_label": ("Low", "Medium", "High")[band - 1] if score is not None else "Unknown",
        "slices": slices,
        "gradient": f"conic-gradient({', '.join(stops)})" if stops else "",
        "mix_state": mix_state,
        "sentiment": sentiment,
        "sentiment_mix": sentiment_mix,
        "sentiment_basis": sentiment_mix["basis"],
        "risk": risk,
        "risk_reading": RISK_READINGS[risk],
        "risk_factors": [
            reason.strip()
            for reason in item.get("risks", [])
            if isinstance(reason, str) and reason.strip()
        ]
        if isinstance(item.get("risks"), list)
        else [],
        "description": (
            f"{attention_text}. {mix_text}. {sentiment_mix['description']} {RISK_READINGS[risk]}."
        ),
    }
