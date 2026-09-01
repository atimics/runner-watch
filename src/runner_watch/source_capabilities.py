from __future__ import annotations

from dataclasses import dataclass

from runner_watch.source_catalog import SourcePolicy


@dataclass(frozen=True, slots=True)
class SourceCapability:
    """One kind of evidence that a scanner source can supply."""

    id: str
    title: str
    description: str
    core: bool = False


SOURCE_CAPABILITIES = (
    SourceCapability(
        "market_universe",
        "Market universe",
        "Symbols, listings, and the companies the scanner can search.",
        core=True,
    ),
    SourceCapability(
        "market_bars",
        "Prices and volume",
        "Daily and intraday bars used to rank movement and liquidity.",
        core=True,
    ),
    SourceCapability(
        "filings",
        "Company filings",
        "Regulatory filings and their source documents.",
    ),
    SourceCapability(
        "fundamentals",
        "Company fundamentals",
        "Financial statements, shares, valuation inputs, and issuer facts.",
    ),
    SourceCapability(
        "market_events",
        "Market events",
        "Trading halts and other timestamped events that can affect a symbol.",
    ),
    SourceCapability(
        "news",
        "Company news",
        "Source-linked headlines and company news metadata.",
    ),
    SourceCapability(
        "social",
        "Public attention",
        "Public social activity and aggregated ticker attention.",
    ),
    SourceCapability(
        "short_data",
        "Short and borrow data",
        "Short interest, borrow fees, and shares available.",
    ),
    SourceCapability(
        "sports_scores",
        "Sports scores and stats",
        "Schedules, scores, leaderboards, and box scores.",
    ),
    SourceCapability(
        "sports_odds",
        "Sports odds",
        "Timestamped market odds and named bookmaker prices.",
    ),
    SourceCapability(
        "sports_news",
        "Sports news",
        "Source-linked team and event news.",
    ),
    SourceCapability(
        "legal_risk",
        "Legal and regulatory risk",
        "Sanctions, enforcement, court, exclusion, and recall evidence.",
    ),
    SourceCapability(
        "entity_context",
        "Entity relationships",
        "Ownership, awards, payments, contributions, and entity links.",
    ),
)

CAPABILITY_BY_ID = {capability.id: capability for capability in SOURCE_CAPABILITIES}

_CAPABILITY_BY_FEED = {
    "universe": "market_universe",
    "company_map": "market_universe",
    "market_bars": "market_bars",
    "current_filings": "filings",
    "filing_index": "filings",
    "filing_document": "filings",
    "document": "filings",
    "company_facts": "fundamentals",
    "trade_halts": "market_events",
    "news_search": "news",
    "reddit_trends": "social",
    "social_search": "social",
    "short_interest": "short_data",
    "borrow_rate": "short_data",
    "sports_scoreboard_preview": "sports_scores",
    "sports_golf_scoreboard_preview": "sports_scores",
    "sports_boxscore_preview": "sports_scores",
    "sports_moneyline_odds": "sports_odds",
    "sports_news_preview": "sports_news",
    "decision_search": "legal_risk",
    "party_search": "legal_risk",
    "recap_search": "legal_risk",
    "sanctions_sdn": "legal_risk",
    "exclusions": "legal_risk",
    "leie": "legal_risk",
    "uscourts_opinions": "legal_risk",
    "enforcement_litigation": "legal_risk",
    "corporate_enforcement": "legal_risk",
    "cases": "legal_risk",
    "enforcement_actions": "legal_risk",
    "enforcement_orders": "legal_risk",
    "disciplinary_actions": "legal_risk",
    "echo_enforcement": "legal_risk",
    "establishment_inspections": "legal_risk",
    "enforcement_recalls": "legal_risk",
    "edis_investigations": "legal_risk",
    "complaints": "legal_risk",
    "entity_relationships": "entity_context",
    "awards": "entity_context",
    "open_payments": "entity_context",
    "contributions": "entity_context",
}


def capability_for_policy(policy: SourcePolicy) -> SourceCapability:
    try:
        capability_id = _CAPABILITY_BY_FEED[policy.feed]
    except KeyError as exc:
        raise ValueError(
            f"Source feed {policy.source}:{policy.feed} needs a scanner capability"
        ) from exc
    return CAPABILITY_BY_ID[capability_id]


def usage_rights_for_policy(policy: SourcePolicy) -> tuple[str, ...]:
    """Return conservative product rights, not a claim about provider ownership."""

    rights = ["local_private"]
    if policy.storage_policy not in {"none", "do_not_store"}:
        rights.append("store_normalized")
    if policy.review_status == "approved" and policy.display_policy not in {
        "internal_only",
        "internal_review_only",
        "review_required",
    }:
        rights.extend(("public_display", "public_derived_signals"))
    return tuple(rights)


def access_model_for_policy(policy: SourcePolicy) -> str:
    if policy.review_status == "poc_only":
        return "experimental"
    if policy.review_status != "approved":
        return "contract_review"
    if policy.credential_env:
        return "bring_your_own"
    return "included"


def assert_catalog_has_capabilities(policies: tuple[SourcePolicy, ...]) -> None:
    for policy in policies:
        capability_for_policy(policy)
