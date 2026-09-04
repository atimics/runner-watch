from __future__ import annotations

from collections.abc import Callable
from typing import Any

PIPELINE_VERSION = "verified-research-v1"

FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "effect": {"type": "string", "enum": ["supports", "risks", "neutral"]},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "source_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["statement", "effect", "evidence_refs", "source_urls"],
                "additionalProperties": False,
            },
        },
        "unknowns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["findings", "unknowns"],
    "additionalProperties": False,
}

CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supported_statements": {"type": "array", "items": {"type": "string"}},
        "rejected_statements": {"type": "array", "items": {"type": "string"}},
        "conflicts": {"type": "array", "items": {"type": "string"}},
        "required_caveats": {"type": "array", "items": {"type": "string"}},
        "verdict": {
            "type": "string",
            "enum": ["strengthened", "weakened", "mixed", "unchanged"],
        },
    },
    "required": [
        "supported_statements",
        "rejected_statements",
        "conflicts",
        "required_caveats",
        "verdict",
    ],
    "additionalProperties": False,
}

SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "thesis": {"type": "string"},
        "summary": {"type": "string"},
        "case_effect": {
            "type": "string",
            "enum": ["strengthened", "weakened", "mixed", "unchanged"],
        },
        "market_view": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "catalysts": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "watch": {"type": "array", "items": {"type": "string"}},
        "unknowns": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "headline",
        "thesis",
        "summary",
        "case_effect",
        "market_view",
        "confidence",
        "catalysts",
        "risks",
        "watch",
        "unknowns",
        "sources",
    ],
    "additionalProperties": False,
}

StageCall = Callable[
    [str, int, str, dict[str, Any], dict[str, Any]],
    tuple[dict[str, Any], dict[str, Any]],
]


def _catalog(context: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    catalog = [
        {
            "ref": "P0",
            "kind": "primary_evidence",
            "observed_at": context.get("primary_evidence", {}).get("captured_at"),
            "source_url": None,
            "data": context.get("primary_evidence", {}),
        }
    ]
    for index, section in enumerate(context.get("context_sections") or [], start=1):
        if not isinstance(section, dict):
            continue
        catalog.append(
            {
                "ref": f"E{index}",
                "kind": section.get("kind"),
                "observed_at": section.get("observed_at"),
                "source_url": section.get("source_url"),
                "data": section.get("data"),
            }
        )
    refs = {str(item["ref"]) for item in catalog}
    urls = {
        str(url)
        for url in context.get("sources") or []
        if isinstance(url, str) and url.startswith("https://")
    }
    return catalog, refs, urls


def _clean_strings(value: Any, *, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for raw in value:
        item = " ".join(str(raw).split())[:500]
        if item and item not in output:
            output.append(item)
        if len(output) >= limit:
            break
    return output


def _verified_review(
    result: dict[str, Any],
    refs: set[str],
    urls: set[str],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for raw in result.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        evidence_refs = [str(ref) for ref in raw.get("evidence_refs") or [] if str(ref) in refs]
        source_urls = [str(url) for url in raw.get("source_urls") or [] if str(url) in urls]
        statement = " ".join(str(raw.get("statement") or "").split())[:800]
        effect = str(raw.get("effect") or "neutral")
        if not statement or not (evidence_refs or source_urls):
            continue
        if effect not in {"supports", "risks", "neutral"}:
            effect = "neutral"
        findings.append(
            {
                "statement": statement,
                "effect": effect,
                "evidence_refs": evidence_refs[:8],
                "source_urls": source_urls[:8],
            }
        )
    return {"findings": findings[:16], "unknowns": _clean_strings(result.get("unknowns"))}


def _call(
    call_stage: StageCall,
    trace: list[dict[str, Any]],
    *,
    stage: str,
    order: int,
    instructions: str,
    payload: dict[str, Any],
    schema: dict[str, Any],
) -> dict[str, Any]:
    result, metadata = call_stage(stage, order, instructions, payload, schema)
    trace.append({"stage": stage, "stage_order": order, **metadata})
    return result


def run_verified_pipeline(
    context: dict[str, Any],
    call_stage: StageCall,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:


    catalog, approved_refs, approved_urls = _catalog(context)
    common = {
        "ticker": context.get("ticker"),
        "user_case": context.get("user_case"),
        "evidence_catalog": catalog,
        "rules": {
            "use_only_catalog_evidence": True,
            "cite_every_finding": True,
            "missing_facts_remain_unknown": True,
        },
    }
    trace: list[dict[str, Any]] = []
    catalyst = _verified_review(
        _call(
            call_stage,
            trace,
            stage="catalyst_researcher",
            order=1,
            instructions=(
                "Find verified business, filing, ownership, and event catalysts. "
                "Do not judge financing safety. Cite evidence refs or supplied URLs."
            ),
            payload=common,
            schema=FINDING_SCHEMA,
        ),
        approved_refs,
        approved_urls,
    )
    financing = _verified_review(
        _call(
            call_stage,
            trace,
            stage="financing_skeptic",
            order=2,
            instructions=(
                "Act as a financing and dilution skeptic. Check offerings, registration, "
                "cash runway, share growth, ownership, and missing terms. Cite every finding."
            ),
            payload=common,
            schema=FINDING_SCHEMA,
        ),
        approved_refs,
        approved_urls,
    )
    market = _verified_review(
        _call(
            call_stage,
            trace,
            stage="market_liquidity_checker",
            order=3,
            instructions=(
                "Check price state, liquidity, volume, VWAP, halt state, and deterministic "
                "risk output. Setup strength never overrides a hard veto. Cite every finding."
            ),
            payload=common,
            schema=FINDING_SCHEMA,
        ),
        approved_refs,
        approved_urls,
    )
    reviews = {"catalyst": catalyst, "financing": financing, "market": market}
    critic = _call(
        call_stage,
        trace,
        stage="independent_critic",
        order=4,
        instructions=(
            "Compare the three reviews. Reject unsupported statements, name conflicts, "
            "keep unknowns, and never weaken a deterministic risk veto."
        ),
        payload={
            "ticker": context.get("ticker"),
            "user_case": context.get("user_case"),
            "reviews": reviews,
            "deterministic_risk": context.get("primary_evidence", {}),
        },
        schema=CRITIC_SCHEMA,
    )
    review_statements = {
        str(finding["statement"])
        for review in reviews.values()
        for finding in review["findings"]
    }
    critic["supported_statements"] = [
        statement
        for statement in _clean_strings(critic.get("supported_statements"), limit=24)
        if statement in review_statements
    ]
    critic["rejected_statements"] = [
        statement
        for statement in _clean_strings(critic.get("rejected_statements"), limit=24)
        if statement in review_statements
    ]
    critic["conflicts"] = _clean_strings(critic.get("conflicts"), limit=12)
    critic["required_caveats"] = _clean_strings(critic.get("required_caveats"), limit=12)
    rejected = set(critic["rejected_statements"])
    critic["supported_statements"] = [
        statement for statement in critic["supported_statements"] if statement not in rejected
    ]
    verified_findings = [
        finding
        for review in reviews.values()
        for finding in review["findings"]
        if finding["statement"] not in rejected
    ]
    verified_unknowns = _clean_strings(
        [
            unknown
            for review in reviews.values()
            for unknown in review["unknowns"]
        ]
        + critic["required_caveats"],
        limit=16,
    )
    synthesis_reviews = {
        name: {
            "findings": [
                finding
                for finding in review["findings"]
                if finding["statement"] not in rejected
            ],
            "unknowns": review["unknowns"],
        }
        for name, review in reviews.items()
    }
    report = _call(
        call_stage,
        trace,
        stage="synthesis",
        order=5,
        instructions=(
            "Write one short report in simple English from the verified reviews and critic. "
            "No hype, no advice, no unsupported claims. Use only supplied source URLs."
        ),
        payload={
            "ticker": context.get("ticker"),
            "user_case": context.get("user_case"),
            "reviews": synthesis_reviews,
            "critic": critic,
            "approved_source_urls": sorted(approved_urls),
        },
        schema=SYNTHESIS_SCHEMA,
    )
    report["catalysts"] = _clean_strings(
        [
            finding["statement"]
            for finding in verified_findings
            if finding["effect"] == "supports"
        ],
        limit=8,
    )
    report["risks"] = _clean_strings(
        [
            finding["statement"]
            for finding in verified_findings
            if finding["effect"] == "risks"
        ],
        limit=8,
    )
    report["watch"] = _clean_strings(
        [
            finding["statement"]
            for finding in verified_findings
            if finding["effect"] == "neutral"
        ]
        + verified_unknowns,
        limit=8,
    )
    report["unknowns"] = verified_unknowns[:8]
    report["sources"] = list(
        dict.fromkeys(
            str(url)
            for finding in verified_findings
            for url in finding["source_urls"]
            if str(url) in approved_urls
        )
    )[:20]
    report["headline"] = " ".join(str(report.get("headline") or "Evidence update").split())[:180]
    report["thesis"] = " ".join(str(report.get("thesis") or "").split())[:2400]
    report["summary"] = " ".join(str(report.get("summary") or "").split())[:1800]
    report["confidence"] = max(0.0, min(1.0, float(report.get("confidence") or 0.0)))
    report["critic"] = critic
    report["pipeline_version"] = PIPELINE_VERSION
    primary = context.get("primary_evidence") or {}
    hard_veto = bool(primary.get("hard_veto"))
    trade_state = str(primary.get("trade_state") or "").upper()
    if hard_veto or trade_state in {"AVOID", "EXIT"}:
        reason = str(primary.get("state_reason") or "Deterministic risk rule is active")
        report["headline"] = f"Risk veto active — {context.get('ticker') or 'ticker'}"
        report["thesis"] = f"Thesis weakened. {reason}"
        report["case_effect"] = "weakened"
        report["market_view"] = "bearish"
        report["confidence"] = max(0.8, report["confidence"])
        if reason not in report["risks"]:
            report["risks"].insert(0, reason)
        warning = "Setup strength cannot override the active risk veto."
        if warning not in report["watch"]:
            report["watch"].insert(0, warning)
        report["deterministic_override"] = True
    else:
        report["deterministic_override"] = False
    return report, trace
