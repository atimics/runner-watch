"""Local, deterministic peer reputation for RATi swarm claims."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, field_validator

from runner_swarm.protocol import NODE_ID_PATTERN, SwarmModel, normalize_utc
from runner_swarm.signed_claim import RunnerObservationV1, SignedClaimV1

PPM = 1_000_000
_CLAIM_ID_PATTERN = r"^sha256:[0-9a-f]{64}$"
_FAMILY_PATTERN = r"^[a-z0-9][a-z0-9._:/-]*$"
SourceFamily = Annotated[str, Field(min_length=1, max_length=48, pattern=_FAMILY_PATTERN)]


class OutcomeVerdict(StrEnum):
    """A result measured by this node, never supplied by the remote peer."""

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


class ClaimOutcomeRecord(SwarmModel):
    """One local measurement of a previously verified runner observation."""

    claim_id: Annotated[str, Field(pattern=_CLAIM_ID_PATTERN)]
    peer_node_id: Annotated[str, Field(min_length=74, max_length=74)]
    measured_at: datetime
    verdict: OutcomeVerdict
    claim_source_families: Annotated[
        tuple[SourceFamily, ...],
        Field(min_length=1, max_length=32),
    ]
    verification_source_families: Annotated[
        tuple[SourceFamily, ...],
        Field(min_length=1, max_length=32),
    ]

    @field_validator("peer_node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("peer_node_id must use the rati-node:<sha256> format")
        return value

    @field_validator("measured_at")
    @classmethod
    def normalize_measured_at(cls, value: datetime) -> datetime:
        return normalize_utc(value, field_name="measured_at")

    @field_validator("claim_source_families", "verification_source_families")
    @classmethod
    def sort_families(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Source families must be unique")
        return tuple(sorted(value))


class ReputationPolicy(SwarmModel):
    """Local scoring choices expressed only as deterministic integers."""

    prior_confirmed: Annotated[int, Field(ge=0, le=1000)] = 1
    prior_refuted: Annotated[int, Field(ge=1, le=1000)] = 3
    minimum_scored_outcomes: Annotated[int, Field(ge=1, le=100_000)] = 20
    minimum_reputation_ppm: Annotated[int, Field(ge=0, le=PPM)] = 600_000
    maximum_influence_ppm: Annotated[int, Field(ge=0, le=PPM)] = 250_000
    target_independent_source_families: Annotated[int, Field(ge=1, le=32)] = 3


class PeerReputation(SwarmModel):
    """A reproducible snapshot derived only from local outcome records."""

    peer_node_id: Annotated[str, Field(min_length=74, max_length=74)]
    confirmed: Annotated[int, Field(ge=0)]
    refuted: Annotated[int, Field(ge=0)]
    inconclusive: Annotated[int, Field(ge=0)]
    reliability_ppm: Annotated[int, Field(ge=0, le=PPM)]
    source_diversity_ppm: Annotated[int, Field(ge=0, le=PPM)]
    reputation_ppm: Annotated[int, Field(ge=0, le=PPM)]
    influence_ppm: Annotated[int, Field(ge=0, le=PPM)]
    eligible: bool

    @property
    def scored_outcomes(self) -> int:
        return self.confirmed + self.refuted


class LocalOutcomeLedger:
    """Small in-memory ledger; callers decide how local records are persisted."""

    def __init__(self) -> None:
        self._records: dict[str, ClaimOutcomeRecord] = {}

    def record(
        self,
        signed_claim: SignedClaimV1,
        *,
        verdict: OutcomeVerdict,
        measured_at: datetime,
        verified_claim_source_families: tuple[str, ...],
        verification_source_families: tuple[str, ...],
    ) -> ClaimOutcomeRecord:
        """Verify identity, then record one locally observed result per claim."""

        signed_claim.verify(at=measured_at, require_current=False)
        claim = signed_claim.claim
        if not isinstance(claim, RunnerObservationV1):
            raise ValueError("Only runner observations can receive outcome measurements")
        measured_at = normalize_utc(measured_at, field_name="measured_at")
        if measured_at < claim.observed_at:
            raise ValueError("measured_at cannot be earlier than the claim observation")
        claimed_families = {item.family for item in claim.evidence}
        if not set(verified_claim_source_families).issubset(claimed_families):
            raise ValueError("Verified source families must be present in the signed claim")

        record = ClaimOutcomeRecord(
            claim_id=signed_claim.claim_id,
            peer_node_id=claim.issuer_node_id,
            measured_at=measured_at,
            verdict=verdict,
            claim_source_families=verified_claim_source_families,
            verification_source_families=verification_source_families,
        )
        previous = self._records.get(record.claim_id)
        if previous is not None and previous != record:
            raise ValueError("A claim already has a different local outcome")
        self._records[record.claim_id] = record
        return record

    def add_record(self, record: ClaimOutcomeRecord) -> ClaimOutcomeRecord:
        """Load a trusted local record without treating peer input as a measurement."""

        previous = self._records.get(record.claim_id)
        if previous is not None and previous != record:
            raise ValueError("A claim already has a different local outcome")
        self._records[record.claim_id] = record
        return record

    def records_for(self, peer_node_id: str) -> tuple[ClaimOutcomeRecord, ...]:
        return tuple(
            sorted(
                (
                    record
                    for record in self._records.values()
                    if record.peer_node_id == peer_node_id
                ),
                key=lambda record: (record.measured_at, record.claim_id),
            )
        )

    def score(
        self,
        peer_node_id: str,
        policy: ReputationPolicy | None = None,
    ) -> PeerReputation:
        return score_peer_reputation(peer_node_id, self.records_for(peer_node_id), policy)


def score_peer_reputation(
    peer_node_id: str,
    records: tuple[ClaimOutcomeRecord, ...],
    policy: ReputationPolicy | None = None,
) -> PeerReputation:
    """Calculate a skeptical posterior discounted for correlated source families."""

    if not NODE_ID_PATTERN.fullmatch(peer_node_id):
        raise ValueError("peer_node_id must use the rati-node:<sha256> format")
    policy = policy or ReputationPolicy()
    if any(record.peer_node_id != peer_node_id for record in records):
        raise ValueError("All outcome records must belong to the scored peer")
    if len({record.claim_id for record in records}) != len(records):
        raise ValueError("Outcome records cannot repeat a claim")

    confirmed = sum(record.verdict == OutcomeVerdict.CONFIRMED for record in records)
    refuted = sum(record.verdict == OutcomeVerdict.REFUTED for record in records)
    inconclusive = sum(record.verdict == OutcomeVerdict.INCONCLUSIVE for record in records)
    scored = confirmed + refuted
    denominator = scored + policy.prior_confirmed + policy.prior_refuted
    reliability_ppm = (confirmed + policy.prior_confirmed) * PPM // denominator

    resolved = tuple(record for record in records if record.verdict != OutcomeVerdict.INCONCLUSIVE)
    family_counts: dict[str, int] = {}
    for record in resolved:
        for family in record.claim_source_families:
            family_counts[family] = family_counts.get(family, 0) + 1
    total_family_uses = sum(family_counts.values())
    if total_family_uses:
        concentration = sum(count * count for count in family_counts.values())
        source_diversity_ppm = min(
            PPM,
            total_family_uses
            * total_family_uses
            * PPM
            // concentration
            // policy.target_independent_source_families,
        )
    else:
        source_diversity_ppm = 0

    reputation_ppm = reliability_ppm * source_diversity_ppm // PPM
    eligible = (
        scored >= policy.minimum_scored_outcomes and reputation_ppm >= policy.minimum_reputation_ppm
    )
    if eligible:
        influence_ppm = policy.maximum_influence_ppm * reputation_ppm // PPM
    else:
        influence_ppm = 0

    return PeerReputation(
        peer_node_id=peer_node_id,
        confirmed=confirmed,
        refuted=refuted,
        inconclusive=inconclusive,
        reliability_ppm=reliability_ppm,
        source_diversity_ppm=source_diversity_ppm,
        reputation_ppm=reputation_ppm,
        influence_ppm=influence_ppm,
        eligible=eligible,
    )
