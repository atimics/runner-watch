from datetime import UTC, datetime, timedelta

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runner_swarm.alpha_pack import (
    AlphaPack,
    NodeIdentity,
    PeerReference,
    SignedAlphaPack,
    sign_alpha_pack,
)
from runner_swarm.node_manifest import (
    NodeEndpoint,
    NodeManifest,
    SignedNodeManifest,
    VersionedDeclaration,
    sign_node_manifest,
    verify_signed_node_manifest,
)
from runner_swarm.protocol import (
    PROTOCOL_VERSION,
    canonical_json_bytes,
    node_id_from_public_key,
    public_key_text,
    signature_domain,
)
from runner_swarm.signed_claim import (
    EvidenceReferenceV1,
    RunnerObservationV1,
    SignedClaimV1,
    SourceVersionV1,
)

NOW = datetime(2026, 8, 29, 18, tzinfo=UTC)
EVIDENCE_ID = "sha256:" + "a" * 64


def test_manifest_claim_and_pack_share_one_node_identity_and_wire_profile() -> None:
    key = Ed25519PrivateKey.from_private_bytes(b"s" * 32)
    public_key = public_key_text(key)
    node_id = node_id_from_public_key(public_key)

    manifest = NodeManifest(
        node_id=node_id,
        public_key=public_key,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        software_name="runner-watch",
        software_version="0.1.0",
        capabilities=(VersionedDeclaration(name="claims.publish", version="1.0.0"),),
        endpoints=(NodeEndpoint(transport="libp2p", address="/dns4/node.example/tcp/443/wss"),),
        schema_versions=(
            VersionedDeclaration(name="rati.signed_claim", version="1.0.0"),
            VersionedDeclaration(name="rati.alpha_pack", version="1.0.0"),
        ),
        supported_topics=("markets/equities/us/runners",),
    )
    signed_manifest = sign_node_manifest(manifest, key)

    claim = RunnerObservationV1(
        issuer_node_id=node_id,
        issuer_public_key=public_key,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        instrument="NASDAQ:PEN",
        observed_at=NOW,
        scanner_version="market_risk_v3",
        schema_version="runner-v1",
        source_versions=(
            SourceVersionV1(family="market", source="massive.bars", version="2026-08"),
        ),
        setup_score_milli=78_500,
        rug_score_milli=23_000,
        rug_level="LOW",
        trade_state="ARMED",
        state_reason="Momentum is building, but the local gate still decides.",
        evidence=(
            EvidenceReferenceV1(
                evidence_id=EVIDENCE_ID,
                family="market",
                source="massive.bars",
                observed_at=NOW,
            ),
        ),
    )
    signed_claim = SignedClaimV1.sign(claim, key)

    owner = NodeIdentity(node_id=node_id, public_key=public_key, display_name="Cloud scanner")
    pack = AlphaPack(
        pack_id="rati-default",
        pack_version=1,
        name="RATi default swarm",
        owner=owner,
        visibility="public",
        issued_at=NOW,
        expires_at=NOW + timedelta(days=30),
        peers=(
            PeerReference(
                **owner.model_dump(),
                roles=("bootstrap", "approved"),
                endpoints=("/dns4/node.example/tcp/443/wss",),
            ),
        ),
        topics=("markets/equities/us/runners",),
        allowed_claim_versions=(PROTOCOL_VERSION,),
        allowed_schema_versions=("runner-v1",),
    )
    signed_pack = sign_alpha_pack(pack, key)

    assert manifest.node_id == claim.issuer_node_id == pack.owner.node_id
    assert manifest.public_key == claim.issuer_public_key == pack.owner.public_key
    assert claim.protocol_version in pack.allowed_claim_versions
    assert claim.schema_version in pack.allowed_schema_versions

    manifest_wire = signed_manifest.to_wire_bytes()
    claim_wire = signed_claim.to_wire_bytes()
    pack_wire = signed_pack.to_wire_bytes()

    restored_manifest = SignedNodeManifest.from_wire_bytes(manifest_wire)
    restored_claim = SignedClaimV1.from_wire_bytes(claim_wire)
    restored_pack = SignedAlphaPack.from_wire_bytes(pack_wire)

    assert verify_signed_node_manifest(restored_manifest, at=NOW) == manifest
    assert restored_claim.verify(at=NOW) == signed_claim
    restored_pack.verify(at=NOW)
    assert restored_manifest.to_wire_bytes() == manifest_wire
    assert restored_claim.to_wire_bytes() == claim_wire
    assert restored_pack.to_wire_bytes() == pack_wire


def test_signature_domains_cannot_be_reused_across_contracts() -> None:
    key = Ed25519PrivateKey.from_private_bytes(b"d" * 32)
    payload = b'{"safe":true}'
    manifest_signature = key.sign(signature_domain("rati.node_manifest") + payload)

    with pytest.raises(InvalidSignature):
        key.public_key().verify(
            manifest_signature,
            signature_domain("rati.signed_claim") + payload,
        )

    with pytest.raises(InvalidSignature):
        key.public_key().verify(
            manifest_signature,
            signature_domain("rati.alpha_pack") + payload,
        )


def test_portable_canonical_json_rejects_floating_point_values() -> None:
    with pytest.raises(ValueError, match="scaled integers"):
        canonical_json_bytes({"score": 78.5})

    assert canonical_json_bytes({"score_milli": 78_500}) == b'{"score_milli":78500}'
