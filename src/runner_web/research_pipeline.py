from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runner_web.research_context import evidence_id_for

PIPELINE_VERSION = "verified-research-v2"

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
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "source_urls": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["statement", "effect", "evidence_ids", "source_urls"],
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


def evidence_catalog(context: dict[str, Any]) -> list[dict[str, Any]]:
    primary = context.get("primary_evidence") or {}
    catalog: list[dict[str, Any]] = []
    if isinstance(primary, dict) and primary:
        primary_observed_at = primary.get("captured_at")
        primary_id = evidence_id_for(
            "primary_evidence",
            primary_observed_at,
            None,
            primary,
        )
        primary_receipt = {
            "receipt_id": primary_id,
            "source_type": "internal_market_snapshot",
            "ticker": context.get("ticker"),
            "observed_at": primary_observed_at,
        }
        catalog.append(
            {
                "evidence_id": primary_id,
                "kind": "primary_evidence",
                "observed_at": primary_observed_at,
                "source_url": None,
                "source_receipt": primary_receipt,
                "data": primary,
            }
        )
    for section in context.get("context_sections") or []:
        if not isinstance(section, dict):
            continue
        kind = str(section.get("kind") or "unknown")
        observed_at = section.get("observed_at")
        source_url = section.get("source_url")
        data = section.get("data")
        evidence_id = evidence_id_for(
            kind,
            observed_at,
            source_url,
            data,
        )
        source_receipt = None
        if source_url is None and kind in {
            "historical_market_assessment",
            "stored_market_bars",
        }:
            source_receipt = {
                "receipt_id": evidence_id,
                "source_type": kind,
                "ticker": context.get("ticker"),
                "observed_at": observed_at,
            }
        catalog.append(
            {
                "evidence_id": evidence_id,
                "kind": kind,
                "observed_at": observed_at,
                "source_url": source_url,
                "source_receipt": source_receipt,
                "data": data,
            }
        )
    return catalog


def _catalog(
    context: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    catalog = evidence_catalog(context)
    by_id = {str(item["evidence_id"]): item for item in catalog}
    urls = {
        str(item["source_url"])
        for item in catalog
        if isinstance(item.get("source_url"), str)
        and str(item["source_url"]).startswith("https://")
    }
    return catalog, by_id, urls


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


def _claim_matches_evidence(statement: str, bound: list[dict[str, Any]]) -> bool:
    text = statement.casefold()
    market_markers = (
        "market price",
        "share price",
        "stock price",
        "trades at",
        "traded at",
        "trading at",
        "trading volume",
        "relative volume",
        "volume",
        "liquidity",
        "vwap",
        "intraday",
        "price momentum",
    )
    if not any(marker in text for marker in market_markers):
        return True
    market_kinds = {
        "primary_evidence",
        "historical_market_assessment",
        "stored_market_bars",
    }
    return any(str(item.get("kind")) in market_kinds for item in bound)


def _verified_review(
    result: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for raw in result.get("findings") or []:
        if not isinstance(raw, dict):
            continue
        requested_ids = list(dict.fromkeys(str(value) for value in raw.get("evidence_ids") or []))
        if not requested_ids or any(evidence_id not in catalog for evidence_id in requested_ids):
            continue
        bound = [catalog[evidence_id] for evidence_id in requested_ids]
        if any(not item.get("source_url") and not item.get("source_receipt") for item in bound):
            continue
        expected_urls = list(
            dict.fromkeys(
                str(item["source_url"])
                for item in bound
                if isinstance(item.get("source_url"), str)
                and str(item["source_url"]).startswith("https://")
            )
        )
        supplied_urls = list(
            dict.fromkeys(
                str(url)
                for url in raw.get("source_urls") or []
                if isinstance(url, str) and url.startswith("https://")
            )
        )
        if set(supplied_urls) != set(expected_urls):
            continue
        statement = " ".join(str(raw.get("statement") or "").split())[:800]
        effect = str(raw.get("effect") or "neutral")
        if not statement or not _claim_matches_evidence(statement, bound):
            continue
        if effect not in {"supports", "risks", "neutral"}:
            effect = "neutral"
        receipts = [
            dict(item["source_receipt"])
            for item in bound
            if isinstance(item.get("source_receipt"), dict)
        ]
        findings.append(
            {
                "statement": statement,
                "effect": effect,
                "evidence_ids": requested_ids[:8],
                "source_urls": expected_urls[:8],
                "source_receipts": receipts[:8],
            }
        )
    return {"findings": findings[:16], "unknowns": _clean_strings(result.get("unknowns"))}


def verified_public_citations(
    citations: Any,
    context: dict[str, Any],
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    _, catalog, _ = _catalog(context)
    findings = []
    for citation in citations if isinstance(citations, list) else []:
        if not isinstance(citation, dict):
            continue
        findings.append(
            {
                "statement": citation.get("claim"),
                "effect": "neutral",
                "evidence_ids": citation.get("evidence_ids"),
                "source_urls": citation.get("source_urls"),
            }
        )
    checked = _verified_review({"findings": findings}, catalog)["findings"]
    return [
        {
            "claim": finding["statement"],
            "evidence_ids": finding["evidence_ids"],
            "source_urls": finding["source_urls"],
            "source_receipts": finding["source_receipts"],
        }
        for finding in checked[:limit]
    ]


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


    catalog, evidence_by_id, approved_urls = _catalog(context)
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
                "Do not judge financing safety. Cite stable evidence IDs and only the exact "
                "source URL attached to each cited item."
            ),
            payload=common,
            schema=FINDING_SCHEMA,
        ),
        evidence_by_id,
    )
    financing = _verified_review(
        _call(
            call_stage,
            trace,
            stage="financing_skeptic",
            order=2,
            instructions=(
                "Act as a financing and dilution skeptic. Check offerings, registration, "
                "cash runway, share growth, ownership, and missing terms. Cite every finding "
                "with stable evidence IDs and their exact attached URLs."
            ),
            payload=common,
            schema=FINDING_SCHEMA,
        ),
        evidence_by_id,
    )
    market = _verified_review(
        _call(
            call_stage,
            trace,
            stage="market_liquidity_checker",
            order=3,
            instructions=(
                "Check price state, liquidity, volume, VWAP, halt state, and deterministic "
                "risk output. Setup strength never overrides a hard veto. Cite stable evidence "
                "IDs. Internal market evidence uses its receipt and an empty URL list."
            ),
            payload=common,
            schema=FINDING_SCHEMA,
        ),
        evidence_by_id,
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
    report["citations"] = [
        {
            "claim": finding["statement"],
            "evidence_ids": finding["evidence_ids"],
            "source_urls": finding["source_urls"],
            "source_receipts": finding["source_receipts"],
        }
        for finding in verified_findings[:30]
    ]
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
