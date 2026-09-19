"""Public assessment facts from saved market evidence."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _probability(value: Any) -> float | None:
    value = _number(value)
    return value if value is not None and 0 <= value <= 1 else None


def _source_url(value: Any) -> str | None:
    text = str(value or "")
    if any(ord(char) < 33 for char in text) or "\\" in text:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme in {"https", "http"} and parsed.netloc and not parsed.username:
        return text
    if text.startswith("/api/") and not parsed.netloc and not parsed.scheme:
        return text
    return None


def _component_key(value: Any) -> str:
    key = str(value or "").lower()
    return key if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", key) else "saved"


def _prediction_reason(team: str, observed: Any) -> str:
    parts = [team] if team else ["Saved model assessment"]
    try:
        moment = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        parts.append(moment.astimezone(UTC).strftime("saved %b %d, %H:%M UTC"))
    except (TypeError, ValueError):
        if team:
            parts.append("saved model estimate")
    return " · ".join(parts)


def assessment(market: str, item: dict[str, Any]) -> dict[str, Any]:
    """Expose saved facts while preserving each market's measurement units."""
    result: dict[str, Any] = {
        "score": _number(item.get("score")),
        "score_detail": None,
        "score_as_of": item.get("score_as_of"),
        "reason": "A saved Runner score is pending.",
        "status": "unknown",
        "label": "Assessment pending",
        "value": None,
        "unit": "",
        "as_of": None,
        "tag": "",
        "tag_tone": "",
        "drivers": [],
        "risks": [],
        "contributions": [],
        "event_impacts": [],
    }
    saved_detail = item.get("score_detail") or {}
    if result["score"] is not None:
        result.update(
            status="saved",
            reason="",
            label="Runner score",
            value=result["score"],
            unit="pts",
            as_of=result["score_as_of"],
        )
        result["score_detail"] = {"score": result["score"], "drivers": [], "penalties": []}
        for group in ("drivers", "penalties"):
            for part in saved_detail.get(group) or []:
                value = _number(part.get("value")) if isinstance(part, dict) else None
                if value is not None:
                    result["score_detail"][group].append(
                        {
                            "key": _component_key(part.get("key")),
                            "label": str(part.get("label") or "Saved contribution"),
                            "value": value,
                        }
                    )
        result["contributions"] = [
            *result["score_detail"]["drivers"],
            *result["score_detail"]["penalties"],
        ]
    if market == "sports":
        prediction = item.get("prediction") or {}
        if not prediction:
            return result
        side = prediction.get("selection")
        probability = (
            _probability(prediction.get(f"{side}_probability"))
            if side in {"home", "away"}
            else None
        )
        signal = str(prediction.get("signal") or "").lower()
        tag, tone = {
            "watch": ("WATCH", "watch"),
            "lean": ("LEAN", "lean"),
            "pass": ("PASS", "pass"),
            "model only": ("MODEL ONLY", "model-only"),
        }.get(signal, ("", ""))
        observed = prediction.get("observed_at")
        team = str(item.get(f"{side}_team_name") or item.get(f"{side}_abbreviation") or "")
        result.update(
            status="saved",
            selected_team=team,
            reason=_prediction_reason(team, observed),
            tag=tag,
            tag_tone=tone,
            selection=side,
            model_version=prediction.get("model_version"),
            quality=prediction.get("quality"),
            risks=[str(value) for value in prediction.get("risks") or []],
        )
        if result["score"] is None:
            result.update(
                label="Model win chance" if probability is not None else "Saved model",
                value=round(probability * 100, 1) if probability is not None else None,
                unit="%" if probability is not None else "",
                as_of=observed,
            )
        # Model and market probabilities are separate measures, not additive score slices.
        for key, label, value, unit in (
            ("model_probability", "Model win chance", probability, "%"),
            (
                "market_probability",
                "Market win chance",
                _probability(prediction.get(f"{side}_market_probability"))
                if side in {"home", "away"}
                else None,
                "%",
            ),
            ("edge", "Model edge", _number(prediction.get("edge")), "pp"),
        ):
            if value is not None:
                result["drivers"].append(
                    {
                        "key": key,
                        "label": label,
                        "value": round(value * 100, 1),
                        "unit": unit,
                        "basis": "saved prediction",
                        "observed_at": observed,
                    }
                )
        result["evidence"] = [str(value) for value in prediction.get("evidence") or []]
        return result

    # The chain analyzer produces sourced observations and patterns. Their units
    # stay intact; the display adds no new weights or numeric risk estimate.
    findings = item.get("findings") or []
    token = str(item.get("token_address") or "")
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if not token or finding.get("token_address") != token:
            continue
        result["drivers"].append(
            {
                "key": _component_key(finding.get("kind") or "observation"),
                "label": str(finding.get("title") or "Saved chain evidence"),
                "value": None,
                "unit": "",
                "basis": str(finding.get("basis") or "observation"),
                "observed_at": finding.get("observed_at"),
                "source_url": _source_url(finding.get("source_url")),
                "explanation": str(finding.get("explanation") or ""),
            }
        )
    result["drivers"] = sorted(
        result["drivers"], key=lambda driver: str(driver.get("observed_at") or ""), reverse=True
    )[:3]
    if result["drivers"] and result["score"] is None:
        result.update(
            status="evidence",
            label="Saved chain evidence",
            as_of=max(str(driver.get("observed_at") or "") for driver in result["drivers"]) or None,
        )
    result["event_impacts"] = [{**driver, "score_impact": None} for driver in result["drivers"]]
    result["freshness"] = "paused" if item.get("stale") else "current"
    return result
