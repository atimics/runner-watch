from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from runner_swarm.alpha_pack import EvidenceRequirements, LocalTrustPolicy
from runner_swarm.protocol import SwarmModel, normalize_utc
from runner_swarm.reputation import PeerReputation
from runner_swarm.signed_claim import (
    ClaimVerificationError,
    RunnerObservationV1,
    SignedClaimV1,
)


class RemoteClaimUse(StrEnum):
    REJECTED = "rejected"
    CONTEXT_ONLY = "context_only"


class RemoteClaimAssessment(SwarmModel):
    claim_id: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    peer_node_id: Annotated[str, Field(min_length=74, max_length=74)]
    use: RemoteClaimUse
    context_weight_ppm: Annotated[int, Field(ge=0, le=1_000_000)] = 0
    source_families: tuple[str, ...] = ()
    reasons: tuple[str, ...]
    can_execute_trade: Literal[False] = False
    trade_command: None = None


def assess_remote_claim(
    signed_claim: SignedClaimV1,
    reputation: PeerReputation,
    *,
    trust_policy: LocalTrustPolicy | None = None,
    evidence_requirements: EvidenceRequirements | None = None,
    local_risk_allows: bool,
    verified_evidence_ids: tuple[str, ...],
    at: datetime | None = None,
) -> RemoteClaimAssessment:

    trust_policy = trust_policy or LocalTrustPolicy()
    evidence_requirements = evidence_requirements or EvidenceRequirements()
    at = normalize_utc(at or datetime.now(UTC), field_name="at")
    reasons: list[str] = []

    try:
        signed_claim.verify(at=at)
    except ClaimVerificationError:
        reasons.append("claim_verification_failed")

    claim = signed_claim.claim
    if reputation.peer_node_id != claim.issuer_node_id:
        reasons.append("reputation_identity_mismatch")
    if len(set(verified_evidence_ids)) != len(verified_evidence_ids):
        reasons.append("duplicate_verified_evidence_id")
    if not isinstance(claim, RunnerObservationV1):
        reasons.append("not_a_runner_observation")
        source_families: tuple[str, ...] = ()
    else:
        evidence_by_id = {item.evidence_id: item for item in claim.evidence}
        unknown_ids = set(verified_evidence_ids) - evidence_by_id.keys()
        if unknown_ids:
            reasons.append("verified_evidence_not_in_claim")
        verified_evidence = tuple(
            evidence_by_id[evidence_id]
            for evidence_id in dict.fromkeys(verified_evidence_ids)
            if evidence_id in evidence_by_id
        )
        source_families = tuple(sorted({item.family for item in verified_evidence}))
        if len(verified_evidence) < evidence_requirements.minimum_receipts:
            reasons.append("not_enough_evidence_receipts")
        if len(source_families) < evidence_requirements.minimum_independent_source_families:
            reasons.append("not_enough_independent_source_families")
        if verified_evidence:
            oldest_evidence_age = max(at - item.observed_at for item in verified_evidence)
            if oldest_evidence_age > timedelta(
                seconds=evidence_requirements.maximum_evidence_age_seconds
            ):
                reasons.append("evidence_too_old")
        if claim.has_hard_veto:
            reasons.append("remote_claim_has_hard_veto")

    if reputation.scored_outcomes < trust_policy.minimum_scored_outcomes:
        reasons.append("not_enough_scored_outcomes")
    if reputation.reputation_ppm < trust_policy.minimum_reputation_ppm:
        reasons.append("reputation_below_local_minimum")
    if not reputation.eligible:
        reasons.append("peer_not_eligible")
    if not local_risk_allows:
        reasons.append("blocked_by_local_risk_gate")

    context_weight_ppm = min(
        reputation.influence_ppm,
        trust_policy.maximum_peer_claim_weight_ppm,
    )
    if context_weight_ppm <= 0:
        reasons.append("no_peer_influence")

    unique_reasons = tuple(dict.fromkeys(reasons))
    if unique_reasons:
        return RemoteClaimAssessment(
            claim_id=signed_claim.claim_id,
            peer_node_id=claim.issuer_node_id,
            use=RemoteClaimUse.REJECTED,
            reasons=unique_reasons,
            source_families=source_families,
        )
    return RemoteClaimAssessment(
        claim_id=signed_claim.claim_id,
        peer_node_id=claim.issuer_node_id,
        use=RemoteClaimUse.CONTEXT_ONLY,
        context_weight_ppm=context_weight_ppm,
        reasons=("remote_claim_is_context_only",),
        source_families=source_families,
    )
