"""Presentation-only stock glyph. Never changes scoring, ordering or run status."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from runner_web.attention import finite_number

GROUPS = (
    ("market", "Market", ("market",)),
    ("evidence", "Filings + news", ("sec_event", "news")),
    ("social", "External social", ("social_search",)),
)
RISK_LEVELS = {"low": 0, "guarded": 1, "medium": 1, "high": 2, "critical": 2}
VERIFICATION_NOTE = (
    "Verified evidence — automated: the existing evidence gate is confirmed and "
    "eligibility checks pass. Not human review, identity verification or an investment endorsement."
)


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _risk(item: Mapping) -> str:
    # Explicit adverse evidence must not be hidden by an inconsistent low label.
    if item.get("hard_veto") or item.get("attention_urgent"):
        return "high"
    level = RISK_LEVELS.get(str(item.get("rug_level") or "").lower())
    score = finite_number(item.get("rug_score"))
    valid = score is not None and 0 <= score <= 100
    if valid:
        numeric = 2 if score >= 50 else 1 if score >= 25 else 0
        return ("low", "medium", "high")[max(numeric, level or 0)]
    if level in (1, 2):
        return ("low", "medium", "high")[level]
    if level == 0 and item.get("rug_score") is None:
        return "low"
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
    for key, label, fields in GROUPS:
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
        for _, _, fields in GROUPS
        for field in fields
    )
    mix_state = "available" if total > 0 else "zero" if supplied and score == 0 else "unknown"
    sentiment = {
        "positive": "positive",
        "risk": "negative",
        "negative": "negative",
        "neutral": "neutral",
        "mixed": "neutral",
    }.get(str(item.get("sentiment") or item.get("catalyst_sentiment") or "").lower(), "unknown")
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
        "sentiment_basis": "Filing sentiment; not price change or forecast probability",
        "risk": risk,
        "description": f"{attention_text}. {mix_text}. Filing sentiment {sentiment}. Risk {risk}.",
        "verification": _verification(item),
    }
