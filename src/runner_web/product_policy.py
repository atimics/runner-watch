from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

PRODUCT_POLICY_VERSION = "stonks.product-policy.v2"


@dataclass(frozen=True, slots=True)
class EvidenceGatePolicy:
    version: int = 2
    mode: str = "independent_families"
    required_family: str = "market"
    threshold: int = 3
    families: tuple[str, ...] = ("market", "primary", "news", "crowd")


@dataclass(frozen=True, slots=True)
class BaseRatePolicy:
    minimum_samples: int = 20
    lookback_days: int = 120
    clock_tolerance_minutes: int = 45


@dataclass(frozen=True, slots=True)
class RankerTrainingPolicy:
    minimum_groups: int = 160
    minimum_rows: int = 5_000
    minimum_per_outcome: int = 20
    maximum_groups: int = 320
    minimum_new_groups: int = 16
    interval_seconds: int = 6 * 60 * 60
    validation_fraction: float = 0.10
    test_fraction: float = 0.10


@dataclass(frozen=True, slots=True)
class ResearchPromotionPolicy:
    learning_cases: int = 20
    promotion_cases: int = 50
    promotion_tickers: int = 10
    minimum_accuracy_lower_bound: float = 0.50
    maximum_brier_score: float = 0.25
    confidence_level: float = 0.95


@dataclass(frozen=True, slots=True)
class OperationsPolicy:
    worker_heartbeat_seconds: int = 30
    worker_heartbeat_max_age_seconds: int = 120
    worker_heartbeat_retire_seconds: int = 600


EVIDENCE_GATE = EvidenceGatePolicy()
BASE_RATES = BaseRatePolicy()
RANKER_TRAINING = RankerTrainingPolicy()
RESEARCH_PROMOTION = ResearchPromotionPolicy()
OPERATIONS = OperationsPolicy()


class SourcePolicyLike(Protocol):
    source: str
    feed: str
    review_status: str
    enabled: bool
    display_policy: str
    product: str


def source_policy_warnings(
    policies: Iterable[SourcePolicyLike],
    *,
    product: str | None = None,
) -> list[dict[str, Any]]:

    return [
        {
            "source": policy.source,
            "feed": policy.feed,
            "review_status": policy.review_status,
            "severity": "blocking" if policy.review_status == "poc_only" else "review",
            "warning": "enabled source has not been approved for public product effects",
        }
        for policy in policies
        if (
            policy.enabled
            and policy.review_status != "approved"
            and policy.display_policy != "internal_review_only"
            and (product is None or policy.product == product)
        )
    ]


def policy_manifest(
    source_policies: Iterable[SourcePolicyLike] = (),
    *,
    product: str | None = None,
) -> dict[str, Any]:

    evidence_gate = asdict(EVIDENCE_GATE)
    evidence_gate["families"] = list(EVIDENCE_GATE.families)
    return {
        "version": PRODUCT_POLICY_VERSION,
        "product": product,
        "evidence_gate": evidence_gate,
        "market_base_rates": asdict(BASE_RATES),
        "ranker_training": asdict(RANKER_TRAINING),
        "research_promotion": asdict(RESEARCH_PROMOTION),
        "operations": asdict(OPERATIONS),
        "source_policy_warnings": source_policy_warnings(
            source_policies,
            product=product,
        ),
    }
