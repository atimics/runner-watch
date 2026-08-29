from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from runner_swarm.alpha_pack import (
    MAX_PACK_BYTES,
    MAX_PEERS,
    MAX_TOPICS,
    AlphaPack,
    AlphaPackError,
    EvidenceRequirements,
    LocalTrustPolicy,
    NodeIdentity,
    PeerReference,
    PrivatePackEncryption,
    SignedAlphaPack,
    node_id_from_public_key,
    public_key_text,
    sign_alpha_pack,
)

NOW = datetime(2026, 8, 29, 18, tzinfo=UTC)


def identity(key: Ed25519PrivateKey, name: str = "Pack owner") -> NodeIdentity:
    public_key = public_key_text(key)
    return NodeIdentity(
        node_id=node_id_from_public_key(public_key),
        signing_public_key=public_key,
        display_name=name,
    )


def make_pack(
    key: Ed25519PrivateKey,
    *,
    visibility: str = "public",
    private_encryption: PrivatePackEncryption | None = None,
    **updates: object,
) -> AlphaPack:
    values: dict[str, object] = {
        "pack_id": "biotech-catalysts",
        "pack_version": 1,
        "name": "Biotech Catalysts",
        "description": "Shared membership and policy, not executable strategy code.",
        "owner": identity(key),
        "visibility": visibility,
        "issued_at": NOW,
        "expires_at": NOW + timedelta(days=30),
        "topics": ["market.biotech", "sec.filings"],
        "allowed_claim_versions": ["1.0"],
        "allowed_schema_versions": ["runner-snapshot/1"],
        "private_encryption": private_encryption,
    }
    values.update(updates)
    return AlphaPack(**values)


def peer(*, roles: tuple[str, ...] = ("approved",)) -> PeerReference:
    key = Ed25519PrivateKey.generate()
    owner = identity(key, "Peer")
    return PeerReference(
        **owner.model_dump(),
        roles=roles,
        endpoints=["/dns4/peer.example/tcp/443/wss"],
    )


def test_signed_pack_round_trips_and_verifies() -> None:
    key = Ed25519PrivateKey.generate()
    pack = make_pack(key, peers=[peer(roles=("bootstrap", "approved"))])

    signed = sign_alpha_pack(pack, key)
    restored = SignedAlphaPack.from_json(signed.canonical_bytes)

    assert restored == signed
    assert restored.pack.owner.node_id == node_id_from_public_key(public_key_text(key))
    restored.verify(at=NOW + timedelta(days=1))


def test_canonical_bytes_and_content_id_are_deterministic() -> None:
    key = Ed25519PrivateKey.generate()
    first = make_pack(
        key,
        topics=["sec.filings", "market.biotech"],
        allowed_claim_versions=["2.0", "1.0"],
    )
    second = make_pack(
        key,
        topics=["market.biotech", "sec.filings"],
        allowed_claim_versions=["1.0", "2.0"],
    )

    assert first.canonical_bytes == second.canonical_bytes
    assert first.content_id == second.content_id
    assert b'": ' not in first.canonical_bytes
    assert b'", "' not in first.canonical_bytes
    assert (
        first.canonical_bytes
        == AlphaPack.model_validate_json(first.canonical_bytes).canonical_bytes
    )


def test_signature_or_content_tampering_is_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    signed = sign_alpha_pack(make_pack(key), key)
    payload = json.loads(signed.canonical_bytes)
    payload["pack"]["description"] = "Tampered"
    tampered = SignedAlphaPack.model_validate(payload)

    with pytest.raises(AlphaPackError, match="content ID"):
        tampered.verify(at=NOW)

    payload = json.loads(signed.canonical_bytes)
    payload["signature"] = ("A" if payload["signature"][0] != "A" else "B") + payload["signature"][
        1:
    ]
    tampered_signature = SignedAlphaPack.model_validate(payload)

    with pytest.raises(AlphaPackError, match="signature"):
        tampered_signature.verify(at=NOW)


def test_expired_and_revoked_packs_are_not_active() -> None:
    key = Ed25519PrivateKey.generate()
    expired = sign_alpha_pack(make_pack(key, expires_at=NOW + timedelta(minutes=1)), key)

    with pytest.raises(AlphaPackError, match="not active"):
        expired.verify(at=NOW + timedelta(minutes=1))

    revoked_pack = make_pack(
        key,
        status="revoked",
        revoked_at=NOW + timedelta(hours=1),
    )
    revoked = sign_alpha_pack(revoked_pack, key)
    with pytest.raises(AlphaPackError, match="not active"):
        revoked.verify(at=NOW + timedelta(hours=2))

    revoked.verify(at=NOW + timedelta(hours=2), require_active=False)


def test_trust_policy_never_turns_membership_into_trust() -> None:
    with pytest.raises(ValidationError, match="membership_grants_trust"):
        LocalTrustPolicy(membership_grants_trust=True)

    with pytest.raises(ValidationError, match="cannot exceed"):
        LocalTrustPolicy(initial_peer_weight=0.2, maximum_peer_claim_weight=0.1)


@pytest.mark.parametrize(
    "policy",
    [
        {"minimum_receipts": 1, "minimum_independent_source_families": 2},
        {"minimum_receipts": 0, "minimum_independent_source_families": 0},
        {"accepted_digest_algorithms": []},
    ],
)
def test_invalid_evidence_policies_are_rejected(policy: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EvidenceRequirements(**policy)


def test_duplicate_peers_topics_and_versions_are_rejected() -> None:
    key = Ed25519PrivateKey.generate()
    repeated_peer = peer()

    with pytest.raises(ValidationError, match="Peer node IDs must be unique"):
        make_pack(key, peers=[repeated_peer, repeated_peer])
    with pytest.raises(ValidationError, match="Topics must be unique"):
        make_pack(key, topics=["sec.filings", "sec.filings"])
    with pytest.raises(ValidationError, match="Version allowlist entries must be unique"):
        make_pack(key, allowed_claim_versions=["1.0", "1.0"])


def test_private_pack_has_metadata_but_rejects_secret_key_fields() -> None:
    key = Ed25519PrivateKey.generate()
    encryption = PrivatePackEncryption(
        group_key_id="quarterly-rotation-7",
        recipient_key_ids=["alice-device-1", "bob-device-2"],
        encrypted_payload_locator="ipfs://bafy-pack-ciphertext",
    )
    pack = make_pack(key, visibility="private", private_encryption=encryption)
    assert pack.private_encryption == encryption

    with pytest.raises(ValidationError, match="Extra inputs"):
        PrivatePackEncryption(
            group_key_id="rotation-8",
            recipient_key_ids=["alice-device-1"],
            secret_key="must-never-appear",
        )

    with pytest.raises(ValidationError, match="require encryption metadata"):
        make_pack(key, visibility="private")


def test_safe_peer_topic_and_endpoint_bounds() -> None:
    key = Ed25519PrivateKey.generate()

    with pytest.raises(ValidationError, match=f"at most {MAX_PEERS} peers"):
        make_pack(key, peers=[peer()] * (MAX_PEERS + 1))
    with pytest.raises(ValidationError, match=f"at most {MAX_TOPICS} topics"):
        make_pack(key, topics=[f"topic.{number}" for number in range(MAX_TOPICS + 1)])
    with pytest.raises(ValidationError, match="at most 8 endpoints"):
        PeerReference(
            **identity(Ed25519PrivateKey.generate()).model_dump(),
            roles=["bootstrap"],
            endpoints=[f"/dns4/peer-{number}.example/tcp/443/wss" for number in range(9)],
        )

    with pytest.raises(AlphaPackError, match="safe input size"):
        SignedAlphaPack.from_json(b"{" + b" " * (MAX_PACK_BYTES + 4096))


def test_identity_binding_signing_key_and_timezone_are_enforced() -> None:
    key = Ed25519PrivateKey.generate()
    wrong_key = Ed25519PrivateKey.generate()
    pack = make_pack(key)

    with pytest.raises(AlphaPackError, match="does not match"):
        sign_alpha_pack(pack, wrong_key)
    with pytest.raises(ValidationError, match="timezone"):
        make_pack(key, issued_at=datetime(2026, 8, 29), expires_at=NOW + timedelta(days=1))
