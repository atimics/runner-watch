from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runner_swarm.local_trust_store import LocalTrustStore, LocalTrustStoreError
from runner_swarm.protocol import node_id_from_public_key
from runner_swarm.remote_policy import RemoteClaimUse, assess_remote_claim
from runner_swarm.reputation import (
    ClaimOutcomeRecord,
    LocalOutcomeLedger,
    OutcomeVerdict,
    ReputationPolicy,
    score_peer_reputation,
)
from runner_swarm.signed_claim import (
    EvidenceReferenceV1,
    RetractionV1,
    RunnerObservationV1,
    SignedClaimV1,
    SourceVersionV1,
    TradeState,
    encode_public_key,
)

NOW = datetime(2026, 8, 30, 16, tzinfo=UTC)


def _observation(
    key: Ed25519PrivateKey,
    *,
    index: int = 1,
    trade_state: TradeState = TradeState.ARMED,
) -> SignedClaimV1:
    public_key = encode_public_key(key.public_key())
    issued_at = NOW + timedelta(seconds=index)
    claim = RunnerObservationV1(
        issuer_node_id=node_id_from_public_key(public_key),
        issuer_public_key=public_key,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=15),
        instrument="NASDAQ:PEN",
        observed_at=issued_at,
        scanner_version="market-risk-v3",
        schema_version="runner-v1",
        source_versions=(
            SourceVersionV1(family="market", source="massive.bars", version="2026-08"),
        ),
        setup_score_milli=80_000,
        rug_score_milli=20_000,
        rug_level="LOW",
        trade_state=trade_state,
        state_reason="Remote analysis is supporting context only.",
        evidence=(
            EvidenceReferenceV1(
                evidence_id=f"sha256:{index:064x}",
                family="market",
                source="massive.bars",
                observed_at=issued_at,
            ),
        ),
    )
    return SignedClaimV1.sign(claim, key)


def _records(
    peer_node_id: str,
    *,
    families: tuple[str, ...],
    confirmed: int,
    refuted: int = 0,
) -> tuple[ClaimOutcomeRecord, ...]:
    verdicts = (OutcomeVerdict.CONFIRMED,) * confirmed + (OutcomeVerdict.REFUTED,) * refuted
    return tuple(
        ClaimOutcomeRecord(
            claim_id=f"sha256:{index:064x}",
            peer_node_id=peer_node_id,
            measured_at=NOW + timedelta(minutes=index),
            verdict=verdict,
            claim_source_families=families,
            verification_source_families=("local-market",),
        )
        for index, verdict in enumerate(verdicts, start=1)
    )


def test_local_ledger_records_one_verified_outcome_per_claim() -> None:
    key = Ed25519PrivateKey.generate()
    signed = _observation(key)
    ledger = LocalOutcomeLedger()

    record = ledger.record(
        signed,
        verdict=OutcomeVerdict.CONFIRMED,
        measured_at=NOW + timedelta(minutes=5),
        verified_claim_source_families=("market",),
        verification_source_families=("local-market", "regulatory"),
    )

    assert record.peer_node_id == signed.claim.issuer_node_id
    assert record.claim_source_families == ("market",)
    assert (
        ledger.record(
            signed,
            verdict=OutcomeVerdict.CONFIRMED,
            measured_at=NOW + timedelta(minutes=5),
            verified_claim_source_families=("market",),
            verification_source_families=("regulatory", "local-market"),
        )
        == record
    )

    with pytest.raises(ValueError, match="different local outcome"):
        ledger.record(
            signed,
            verdict=OutcomeVerdict.REFUTED,
            measured_at=NOW + timedelta(minutes=5),
            verified_claim_source_families=("market",),
            verification_source_families=("local-market",),
        )

    other = _observation(key, index=2)
    with pytest.raises(ValueError, match="present in the signed claim"):
        ledger.record(
            other,
            verdict=OutcomeVerdict.CONFIRMED,
            measured_at=NOW + timedelta(minutes=5),
            verified_claim_source_families=("social",),
            verification_source_families=("local-market",),
        )


def test_reputation_is_conservative_and_penalizes_correlated_source_families() -> None:
    key = Ed25519PrivateKey.generate()
    peer_node_id = node_id_from_public_key(key)
    policy = ReputationPolicy()

    unknown = score_peer_reputation(peer_node_id, (), policy)
    concentrated = score_peer_reputation(
        peer_node_id,
        _records(peer_node_id, families=("market",), confirmed=20),
        policy,
    )
    diverse = score_peer_reputation(
        peer_node_id,
        _records(
            peer_node_id,
            families=("market", "news", "regulatory"),
            confirmed=20,
        ),
        policy,
    )

    assert unknown.reputation_ppm == 0
    assert unknown.influence_ppm == 0
    assert concentrated.reliability_ppm == diverse.reliability_ppm == 875_000
    assert concentrated.source_diversity_ppm == 333_333
    assert concentrated.eligible is False
    assert concentrated.influence_ppm == 0
    assert diverse.source_diversity_ppm == 1_000_000
    assert diverse.reputation_ppm == 875_000
    assert diverse.influence_ppm == 218_750
    assert diverse.eligible is True


def test_reputation_is_independent_of_record_order_and_ignores_inconclusive_results() -> None:
    key = Ed25519PrivateKey.generate()
    peer_node_id = node_id_from_public_key(key)
    records = _records(
        peer_node_id,
        families=("market", "news", "regulatory"),
        confirmed=18,
        refuted=2,
    )
    inconclusive = ClaimOutcomeRecord(
        claim_id="sha256:" + "f" * 64,
        peer_node_id=peer_node_id,
        measured_at=NOW,
        verdict=OutcomeVerdict.INCONCLUSIVE,
        claim_source_families=("social",),
        verification_source_families=("local-market",),
    )

    forward = score_peer_reputation(peer_node_id, records + (inconclusive,))
    reverse = score_peer_reputation(peer_node_id, tuple(reversed(records)) + (inconclusive,))

    assert forward == reverse
    assert forward.confirmed == 18
    assert forward.refuted == 2
    assert forward.inconclusive == 1
    assert forward.reliability_ppm == 791_666


def test_remote_trade_state_can_only_be_used_as_weighted_context() -> None:
    key = Ed25519PrivateKey.generate()
    signed = _observation(key, trade_state=TradeState.TRIGGERED)
    peer_node_id = signed.claim.issuer_node_id
    reputation = score_peer_reputation(
        peer_node_id,
        _records(
            peer_node_id,
            families=("market", "news", "regulatory"),
            confirmed=20,
        ),
    )

    assessment = assess_remote_claim(
        signed,
        reputation,
        local_risk_allows=True,
        verified_evidence_ids=(signed.claim.evidence[0].evidence_id,),
        at=NOW + timedelta(minutes=1),
    )

    assert assessment.use == RemoteClaimUse.CONTEXT_ONLY
    assert assessment.context_weight_ppm == 218_750
    assert assessment.can_execute_trade is False
    assert assessment.trade_command is None
    assert "TRIGGERED" not in assessment.model_dump_json()


def test_local_risk_and_authenticity_gates_reject_remote_context() -> None:
    key = Ed25519PrivateKey.generate()
    signed = _observation(key)
    peer_node_id = signed.claim.issuer_node_id
    reputation = score_peer_reputation(
        peer_node_id,
        _records(
            peer_node_id,
            families=("market", "news", "regulatory"),
            confirmed=20,
        ),
    )

    blocked = assess_remote_claim(
        signed,
        reputation,
        local_risk_allows=False,
        verified_evidence_ids=(signed.claim.evidence[0].evidence_id,),
        at=NOW + timedelta(minutes=1),
    )
    assert blocked.use == RemoteClaimUse.REJECTED
    assert blocked.context_weight_ppm == 0
    assert "blocked_by_local_risk_gate" in blocked.reasons

    unverified = assess_remote_claim(
        signed,
        reputation,
        local_risk_allows=True,
        verified_evidence_ids=(),
        at=NOW + timedelta(minutes=1),
    )
    assert unverified.use == RemoteClaimUse.REJECTED
    assert "not_enough_evidence_receipts" in unverified.reasons

    tampered = signed.model_copy(
        update={"claim": signed.claim.model_copy(update={"setup_score_milli": 99_000})}
    )
    rejected = assess_remote_claim(
        tampered,
        reputation,
        local_risk_allows=True,
        verified_evidence_ids=(signed.claim.evidence[0].evidence_id,),
        at=NOW + timedelta(minutes=1),
    )
    assert rejected.use == RemoteClaimUse.REJECTED
    assert "claim_verification_failed" in rejected.reasons


def test_retractions_are_not_trade_context() -> None:
    key = Ed25519PrivateKey.generate()
    observation = _observation(key)
    public_key = encode_public_key(key.public_key())
    retraction = SignedClaimV1.sign(
        RetractionV1(
            issuer_node_id=node_id_from_public_key(public_key),
            issuer_public_key=public_key,
            issued_at=NOW + timedelta(minutes=1),
            expires_at=NOW + timedelta(days=1),
            target_claim_id=observation.claim_id,
            reason="The local source was stale.",
        ),
        key,
    )
    reputation = score_peer_reputation(
        observation.claim.issuer_node_id,
        _records(
            observation.claim.issuer_node_id,
            families=("market", "news", "regulatory"),
            confirmed=20,
        ),
    )

    assessment = assess_remote_claim(
        retraction,
        reputation,
        local_risk_allows=True,
        verified_evidence_ids=(),
        at=NOW + timedelta(minutes=2),
    )

    assert assessment.use == RemoteClaimUse.REJECTED
    assert "not_a_runner_observation" in assessment.reasons


def test_sqlite_outcomes_survive_restart_and_remain_idempotent(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    signed = _observation(key)
    database_path = tmp_path / "swarm-local-trust.sqlite3"

    with LocalTrustStore(database_path) as store:
        record = store.record_outcome(
            signed,
            verdict=OutcomeVerdict.CONFIRMED,
            measured_at=NOW + timedelta(minutes=5),
            verified_claim_source_families=("market",),
            verification_source_families=("local-market",),
        )
        assert store.add_outcome(record) == record

    with LocalTrustStore(database_path) as restored:
        assert restored.outcomes_for(record.peer_node_id) == (record,)
        assert restored.score(record.peer_node_id).confirmed == 1
        conflicting = record.model_copy(update={"verdict": OutcomeVerdict.REFUTED})
        with pytest.raises(LocalTrustStoreError, match="different local outcome"):
            restored.add_outcome(conflicting)


def test_sqlite_outcome_retention_is_bounded_and_deterministic(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    peer_node_id = node_id_from_public_key(key)
    records = _records(
        peer_node_id,
        families=("market", "news", "regulatory"),
        confirmed=3,
    )

    with LocalTrustStore(
        tmp_path / "bounded-trust.sqlite3",
        max_outcomes_per_peer=2,
        max_total_outcomes=3,
    ) as store:
        for record in reversed(records):
            store.add_outcome(record)

        assert store.outcomes_for(peer_node_id) == records[-2:]
        assert store.score(peer_node_id).confirmed == 2
