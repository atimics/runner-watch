from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

ROADMAP_VERSION = "stonks.roadmap.v1"
ROADMAP_REVIEWED_AT = "2026-08-26"


@dataclass(frozen=True, slots=True)
class RoadmapItem:
    key: str
    title: str
    summary: str
    decision: str
    status: str


ROADMAP_ITEMS = (
    RoadmapItem(
        "pulse-risk",
        "Pulse, rug risk, and trade state",
        "Find unusual movement, then block weak or dangerous setups before they look bullish.",
        "own",
        "live",
    ),
    RoadmapItem(
        "sec-evidence",
        "SEC filings and issuer risk",
        "Turn filings and company facts into source-linked ownership, dilution, "
        "and runway evidence.",
        "own",
        "live",
    ),
    RoadmapItem(
        "social-loop",
        "Radar and Alpha",
        "Follow fresh Pulse events, comments, and the ranked Alpha view.",
        "own",
        "live",
    ),
    RoadmapItem(
        "outcome-loop",
        "Outcome receipts",
        "Freeze calls and measure what happened without rewriting the original view.",
        "own",
        "live",
    ),
    RoadmapItem(
        "flash-wallet",
        "Flash credit wallet",
        "Claim 100 Flash each day, lock one ticker's daily alpha for an hour, or publish it early.",
        "own",
        "live",
    ),
    RoadmapItem(
        "ranker",
        "Calibrated runner model",
        "Keep collecting complete outcomes until the shadow model passes its promotion rules.",
        "own",
        "learning",
    ),
    RoadmapItem(
        "ai-scorecards",
        "Measured AI research",
        "Score Flash by real outcomes and promote policies only after enough independent cases.",
        "own",
        "learning",
    ),
    RoadmapItem(
        "quote-pilot",
        "Licensed quote pilot",
        "Compare a licensed feed with Yahoo for spread, freshness, coverage, and data rights.",
        "buy",
        "now",
    ),
    RoadmapItem(
        "halt-review",
        "Trading halt approval",
        "Finish the Nasdaq feed review before the existing halt safety path is treated as public.",
        "buy",
        "now",
    ),
    RoadmapItem(
        "radar-personal",
        "Personal Radar",
        "Do not profile what a person reads or acts on. Keep Radar shared and unpersonalized.",
        "cut",
        "cut",
    ),
    RoadmapItem(
        "biotech",
        "Biotech catalyst calendar",
        "Link trial and FDA changes to public companies with reviewed, source-backed matches.",
        "copy",
        "next",
    ),
    RoadmapItem(
        "licensed-news",
        "Licensed news and corporate actions",
        "Buy reliable metadata and display rights after the quote pilot proves the need.",
        "buy",
        "next",
    ),
    RoadmapItem(
        "crowding",
        "Dated crowding evidence",
        "Add FINRA short interest and short-sale volume as separate facts, "
        "never as a squeeze promise.",
        "buy",
        "later",
    ),
    RoadmapItem(
        "options",
        "Options flow and dealer analytics",
        "The cost and weak penny-stock coverage do not support the current product.",
        "cut",
        "cut",
    ),
    RoadmapItem(
        "portfolio",
        "Broker linking and portfolio optimization",
        "Keep the private manual trade journal; do not become a portfolio manager.",
        "cut",
        "cut",
    ),
    RoadmapItem(
        "broad-tools",
        "Dividend, analyst, fund, and custom quant tools",
        "These features pull the product away from fast low-priced stock intelligence.",
        "cut",
        "cut",
    ),
)


def roadmap_snapshot() -> dict[str, Any]:
    items = [asdict(item) for item in ROADMAP_ITEMS]
    order = ("live", "ready", "learning", "now", "next", "later", "cut")
    groups = [
        {"status": status, "items": [item for item in items if item["status"] == status]}
        for status in order
    ]
    return {
        "version": ROADMAP_VERSION,
        "reviewed_at": ROADMAP_REVIEWED_AT,
        "promise": "Find the move, explain why, show the rug risk, and record what happened.",
        "groups": [group for group in groups if group["items"]],
    }
