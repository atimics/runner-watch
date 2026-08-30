from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from runner_swarm.protocol import node_id_from_public_key
from runner_swarm.signed_claim import (
    ClaimExpiredError,
    ClaimVerificationError,
    EvidenceReferenceV1,
    RetractionV1,
    RiskSeverity,
    RiskVetoV1,
    RunnerObservationV1,
    SignedClaimV1,
    SourceVersionV1,
    TradeState,
    encode_public_key,
)

NOW = datetime(2026, 8, 29, 16, tzinfo=UTC)
EVIDENCE_ID = "sha256:" + "a" * 64


def observation(
    private_key: Ed25519PrivateKey,
    *,
    supersedes_claim_id: str | None = None,
    expires_at: datetime | None = None,
) -> RunnerObservationV1:
    return RunnerObservationV1(
        issuer_node_id=node_id_from_public_key(encode_public_key(private_key.public_key())),
        issuer_public_key=encode_public_key(private_key.public_key()),
        issued_at=NOW,
        expires_at=expires_at or NOW + timedelta(minutes=15),
        instrument="NASDAQ:PEN",
        observed_at=NOW - timedelta(seconds=2),
        scanner_version="market_risk_v3",
        schema_version="runner-v1",
        source_versions=(
            SourceVersionV1(family="market", source="massive.bars", version="2026-08"),
            SourceVersionV1(family="regulatory", source="sec.filings", version="v1"),
        ),
        setup_score_milli=78_500,
        rug_score_milli=23_000,
        rug_level="LOW",
        trade_state=TradeState.ARMED,
        state_reason="Momentum is building, but the reclaim still needs proof.",
        signals=("relative-volume", "vwap-reclaim"),
        risk_vetoes=(
            RiskVetoV1(
                code="offering-risk",
                reason="A recent shelf registration needs review.",
                severity=RiskSeverity.WARNING,
                evidence_ids=(EVIDENCE_ID,),
            ),
        ),
        evidence=(
            EvidenceReferenceV1(
                evidence_id=EVIDENCE_ID,
                family="regulatory",
                source="sec.filings",
                observed_at=NOW - timedelta(minutes=2),
                locator="https://www.sec.gov/Archives/example.txt",
            ),
        ),
        supersedes_claim_id=supersedes_claim_id,
    )


def test_signed_claim_round_trips_as_canonical_wire_json() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = SignedClaimV1.sign(observation(private_key), private_key)

    wire_bytes = signed.to_wire_bytes()
    restored = SignedClaimV1.from_wire_bytes(wire_bytes)

    assert restored == signed
    assert restored.verify(at=NOW + timedelta(minutes=1)) is restored
    assert b'": ' not in wire_bytes
    assert b', "' not in wire_bytes
    assert restored.claim.issued_at.tzinfo is UTC


def test_content_bytes_and_claim_id_are_deterministic() -> None:
    private_key = Ed25519PrivateKey.generate()
    claim = observation(private_key)

    first = SignedClaimV1.sign(claim, private_key)
    second = SignedClaimV1.sign(claim, private_key)

    assert first.claim_bytes() == second.claim_bytes()
    assert first.claim_id == second.claim_id
    assert first.signature == second.signature
    assert first.to_wire_bytes() == second.to_wire_bytes()


def test_verification_rejects_content_and_signature_tampering() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = SignedClaimV1.sign(observation(private_key), private_key)
    changed_claim = signed.claim.model_copy(update={"setup_score_milli": 99_000})

    wrong_id = signed.model_copy(update={"claim": changed_claim})
    with pytest.raises(ClaimVerificationError, match="claim_id"):
        wrong_id.verify(at=NOW)

    changed_id = wrong_id.model_copy(update={"claim_id": wrong_id.expected_claim_id()})
    with pytest.raises(ClaimVerificationError, match="signature"):
        changed_id.verify(at=NOW)


def test_expired_claim_keeps_a_valid_signature_but_is_not_current() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = SignedClaimV1.sign(
        observation(private_key, expires_at=NOW + timedelta(minutes=1)), private_key
    )

    assert signed.verify_signature() is True
    assert signed.is_expired(NOW + timedelta(minutes=1)) is True
    with pytest.raises(ClaimExpiredError):
        signed.verify(at=NOW + timedelta(minutes=1))


def test_models_reject_unsafe_timestamps_counts_and_references() -> None:
    private_key = Ed25519PrivateKey.generate()
    values = observation(private_key).model_dump()

    with pytest.raises(ValidationError, match="timezone"):
        RunnerObservationV1(**{**values, "observed_at": datetime(2026, 8, 29)})

    with pytest.raises(ValidationError, match="24 hours"):
        RunnerObservationV1(**{**values, "expires_at": NOW + timedelta(hours=24, seconds=1)})

    with pytest.raises(ValidationError, match="more than 24 hours before"):
        RunnerObservationV1(**{**values, "observed_at": NOW - timedelta(days=1, seconds=1)})

    future_evidence = EvidenceReferenceV1(
        **{
            **values["evidence"][0],
            "observed_at": NOW + timedelta(minutes=6),
        }
    )
    with pytest.raises(ValidationError, match="Evidence cannot be observed"):
        RunnerObservationV1(**{**values, "evidence": (future_evidence,)})

    too_many = tuple(values["evidence"][0] for _ in range(33))
    with pytest.raises(ValidationError, match="at most 32"):
        RunnerObservationV1(**{**values, "evidence": too_many})

    unknown_id = "sha256:" + "b" * 64
    invalid_veto = RiskVetoV1(
        code="unknown-evidence",
        reason="This reference is not in the claim.",
        evidence_ids=(unknown_id,),
    )
    with pytest.raises(ValidationError, match="included in the claim"):
        RunnerObservationV1(**{**values, "risk_vetoes": (invalid_veto,)})

    with pytest.raises(ValidationError):
        RunnerObservationV1(**{**values, "setup_score_milli": 78.5})


def test_signed_content_has_a_hard_byte_limit() -> None:
    private_key = Ed25519PrivateKey.generate()
    values = observation(private_key).model_dump()
    large_evidence = tuple(
        EvidenceReferenceV1(
            evidence_id=f"sha256:{index:064x}",
            family="alternative",
            source=f"large-source-{index}",
            observed_at=NOW,
            locator="urn:rati:" + "x" * 500,
        )
        for index in range(32)
    )
    large_sources = tuple(
        SourceVersionV1(
            family="alternative",
            source=f"source-{index}-" + "x" * 80,
            version="v" * 64,
        )
        for index in range(32)
    )
    large_claim = RunnerObservationV1(
        **{
            **values,
            "evidence": large_evidence,
            "risk_vetoes": (),
            "source_versions": large_sources,
        }
    )

    with pytest.raises(ValueError, match="too large"):
        SignedClaimV1.sign(large_claim, private_key)


def test_models_are_frozen_and_reject_unknown_fields() -> None:
    private_key = Ed25519PrivateKey.generate()
    claim = observation(private_key)

    with pytest.raises(ValidationError, match="frozen"):
        claim.setup_score_milli = 10_000

    with pytest.raises(ValidationError, match="Extra inputs"):
        RunnerObservationV1(**claim.model_dump(), command="BUY")


def test_retraction_only_applies_to_the_same_signer_and_exact_claim() -> None:
    owner = Ed25519PrivateKey.generate()
    stranger = Ed25519PrivateKey.generate()
    original = SignedClaimV1.sign(observation(owner), owner)
    retraction_content = RetractionV1(
        issuer_node_id=node_id_from_public_key(encode_public_key(owner.public_key())),
        issuer_public_key=encode_public_key(owner.public_key()),
        issued_at=NOW + timedelta(minutes=2),
        expires_at=NOW + timedelta(days=1),
        target_claim_id=original.claim_id,
        reason="The source timestamp was wrong.",
    )
    retraction = SignedClaimV1.sign(retraction_content, owner)

    assert retraction.retracts(original, at=NOW + timedelta(minutes=3)) is True

    stranger_content = retraction_content.model_copy(
        update={
            "issuer_node_id": node_id_from_public_key(encode_public_key(stranger.public_key())),
            "issuer_public_key": encode_public_key(stranger.public_key()),
        }
    )
    stranger_retraction = SignedClaimV1.sign(stranger_content, stranger)
    assert stranger_retraction.retracts(original, at=NOW + timedelta(minutes=3)) is False


def test_supersession_only_links_verified_claims_from_the_same_signer() -> None:
    owner = Ed25519PrivateKey.generate()
    original = SignedClaimV1.sign(observation(owner), owner)
    replacement_content = observation(owner, supersedes_claim_id=original.claim_id).model_copy(
        update={"issued_at": NOW + timedelta(minutes=1)}
    )
    replacement = SignedClaimV1.sign(replacement_content, owner)

    assert replacement.supersedes(original, at=NOW + timedelta(minutes=2)) is True

    other_instrument = replacement_content.model_copy(update={"instrument": "NASDAQ:OTHER"})
    assert (
        SignedClaimV1.sign(other_instrument, owner).supersedes(
            original, at=NOW + timedelta(minutes=2)
        )
        is False
    )


def test_private_key_must_match_the_claim_identity() -> None:
    owner = Ed25519PrivateKey.generate()
    stranger = Ed25519PrivateKey.generate()

    with pytest.raises(ValueError, match="does not match"):
        SignedClaimV1.sign(observation(owner), stranger)


def test_noncanonical_wire_json_is_rejected() -> None:
    private_key = Ed25519PrivateKey.generate()
    signed = SignedClaimV1.sign(observation(private_key), private_key)
    pretty_wire = signed.model_dump_json(indent=2).encode()

    with pytest.raises(ValueError, match="canonical"):
        SignedClaimV1.from_wire_bytes(pretty_wire)
