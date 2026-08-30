from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from runner_swarm.key_rotation import (
    KeyRotationError,
    KeyRotationV1,
    LocalKeyRotationRegistry,
    RotationDecisionStatus,
    SignedKeyRotationV1,
)
from runner_swarm.local_trust_store import LocalTrustStore, LocalTrustStoreError
from runner_swarm.protocol import NodeIdentity, node_id_from_public_key, public_key_text
from runner_swarm.reputation import ClaimOutcomeRecord, OutcomeVerdict

NOW = datetime(2026, 8, 30, 18, tzinfo=UTC)


def _identity(key: Ed25519PrivateKey, name: str) -> NodeIdentity:
    return NodeIdentity(
        node_id=node_id_from_public_key(key),
        public_key=public_key_text(key),
        display_name=name,
    )


def _rotation(
    old_key: Ed25519PrivateKey,
    new_key: Ed25519PrivateKey,
    *,
    sequence: int = 1,
) -> SignedKeyRotationV1:
    rotation = KeyRotationV1(
        old_identity=_identity(old_key, "Old scanner"),
        new_identity=_identity(new_key, "New scanner"),
        sequence=sequence,
        issued_at=NOW,
        effective_at=NOW + timedelta(minutes=5),
        expires_at=NOW + timedelta(days=7),
        reason="Routine local key rotation.",
    )
    return SignedKeyRotationV1.sign(rotation, old_key, new_key)


def test_rotation_requires_both_keys_and_round_trips_canonical_wire_json() -> None:
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    stranger = Ed25519PrivateKey.generate()
    signed = _rotation(old_key, new_key)

    restored = SignedKeyRotationV1.from_wire_bytes(signed.to_wire_bytes())

    assert restored == signed
    assert restored.verify(at=NOW + timedelta(minutes=1)) is restored
    assert restored.to_wire_bytes() == signed.to_wire_bytes()
    with pytest.raises(ValueError, match="new_private_key"):
        SignedKeyRotationV1.sign(signed.rotation, old_key, stranger)


def test_rotation_rejects_tampering_expiry_and_identity_reuse() -> None:
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    signed = _rotation(old_key, new_key)
    tampered = signed.model_copy(
        update={"rotation": signed.rotation.model_copy(update={"reason": "Changed reason."})}
    )

    with pytest.raises(KeyRotationError, match="content ID"):
        tampered.verify(at=NOW + timedelta(minutes=1))
    resigned_id_only = tampered.model_copy(update={"content_id": tampered.rotation.content_id})
    with pytest.raises(KeyRotationError, match="signatures"):
        resigned_id_only.verify(at=NOW + timedelta(minutes=1))
    with pytest.raises(KeyRotationError, match="expired"):
        signed.verify(at=NOW + timedelta(days=7))

    values = signed.rotation.model_dump()
    with pytest.raises(ValidationError, match="must change"):
        KeyRotationV1(
            **{
                **values,
                "new_identity": values["old_identity"],
            }
        )


def test_local_registry_never_transfers_continuity_without_acceptance() -> None:
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    signed = _rotation(old_key, new_key)
    old_node_id = signed.rotation.old_identity.node_id
    new_node_id = signed.rotation.new_identity.node_id
    registry = LocalKeyRotationRegistry()

    assert registry.resolve(old_node_id) == old_node_id
    assert registry.is_continuation(old_node_id, new_node_id) is False
    with pytest.raises(KeyRotationError, match="effective_at"):
        registry.accept(
            signed,
            decided_at=NOW + timedelta(minutes=1),
            reason="Too early.",
        )

    rejected = registry.reject(
        signed,
        decided_at=NOW + timedelta(minutes=1),
        reason="Unexpected key change.",
    )
    assert rejected.status == RotationDecisionStatus.REJECTED
    assert registry.resolve(old_node_id) == old_node_id

    accepted = registry.accept(
        signed,
        decided_at=NOW + timedelta(minutes=6),
        reason="Confirmed through a separate local channel.",
    )
    assert accepted.status == RotationDecisionStatus.ACCEPTED
    assert registry.resolve(old_node_id) == new_node_id
    assert registry.is_continuation(old_node_id, new_node_id) is True
    assert [item.status for item in registry.history] == [
        RotationDecisionStatus.REJECTED,
        RotationDecisionStatus.ACCEPTED,
    ]


def test_revocation_stops_local_identity_continuity() -> None:
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    signed = _rotation(old_key, new_key)
    registry = LocalKeyRotationRegistry()
    registry.accept(
        signed,
        decided_at=NOW + timedelta(minutes=6),
        reason="Operator confirmed rotation.",
    )

    revoked = registry.revoke(
        signed.content_id,
        decided_at=NOW + timedelta(minutes=7),
        reason="Replacement key was compromised.",
    )

    assert revoked.status == RotationDecisionStatus.REVOKED
    assert registry.resolve(signed.rotation.old_identity.node_id) == (
        signed.rotation.old_identity.node_id
    )
    assert (
        registry.is_continuation(
            signed.rotation.old_identity.node_id,
            signed.rotation.new_identity.node_id,
        )
        is False
    )
    with pytest.raises(KeyRotationError, match="accepted"):
        registry.revoke(
            signed.content_id,
            decided_at=NOW + timedelta(minutes=8),
            reason="Duplicate revocation.",
        )


def test_registry_supports_chains_and_rejects_forks_and_cycles() -> None:
    first_key = Ed25519PrivateKey.generate()
    second_key = Ed25519PrivateKey.generate()
    third_key = Ed25519PrivateKey.generate()
    fork_key = Ed25519PrivateKey.generate()
    registry = LocalKeyRotationRegistry()
    first = _rotation(first_key, second_key, sequence=1)
    second = _rotation(second_key, third_key, sequence=2)
    registry.accept(first, decided_at=NOW + timedelta(minutes=6), reason="First rotation.")
    registry.accept(second, decided_at=NOW + timedelta(minutes=7), reason="Second rotation.")

    assert registry.resolve(first.rotation.old_identity.node_id) == (
        second.rotation.new_identity.node_id
    )

    fork = _rotation(first_key, fork_key, sequence=2)
    with pytest.raises(KeyRotationError, match="already has"):
        registry.accept(fork, decided_at=NOW + timedelta(minutes=8), reason="Fork attempt.")

    cycle = _rotation(third_key, first_key, sequence=3)
    with pytest.raises(KeyRotationError, match="cycle"):
        registry.accept(cycle, decided_at=NOW + timedelta(minutes=8), reason="Cycle attempt.")


def test_sqlite_rotation_decisions_survive_restart_and_revocation(tmp_path: Path) -> None:
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    signed = _rotation(old_key, new_key)
    old_node_id = signed.rotation.old_identity.node_id
    new_node_id = signed.rotation.new_identity.node_id
    database_path = tmp_path / "rotation-trust.sqlite3"

    with LocalTrustStore(database_path) as store:
        store.add_outcome(
            ClaimOutcomeRecord(
                claim_id="sha256:" + "a" * 64,
                peer_node_id=old_node_id,
                measured_at=NOW,
                verdict=OutcomeVerdict.CONFIRMED,
                claim_source_families=("market", "news", "regulatory"),
                verification_source_families=("local-market",),
            )
        )
        store.reject_rotation(
            signed,
            decided_at=NOW + timedelta(minutes=1),
            reason="Waiting for local confirmation.",
        )
        assert store.resolve_node_id(old_node_id) == old_node_id
        assert store.score(new_node_id).confirmed == 0

    with LocalTrustStore(database_path) as restored:
        restored.accept_rotation(
            signed,
            decided_at=NOW + timedelta(minutes=6),
            reason="Confirmed locally.",
        )
        assert restored.resolve_node_id(old_node_id) == new_node_id
        assert restored.score(new_node_id).confirmed == 1

    with LocalTrustStore(database_path) as restored:
        assert restored.is_continuation(old_node_id, new_node_id) is True
        restored.revoke_rotation(
            signed.content_id,
            decided_at=NOW + timedelta(minutes=7),
            reason="Replacement key was compromised.",
        )

    with LocalTrustStore(database_path) as restored:
        assert restored.resolve_node_id(old_node_id) == old_node_id
        assert restored.score(new_node_id).confirmed == 0
        assert restored.score(old_node_id).confirmed == 1
        assert [item.status for item in restored.rotation_registry().history] == [
            RotationDecisionStatus.REJECTED,
            RotationDecisionStatus.ACCEPTED,
            RotationDecisionStatus.REVOKED,
        ]


def test_sqlite_rotation_limit_rolls_back_the_new_decision(tmp_path: Path) -> None:
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    signed = _rotation(old_key, new_key)
    old_node_id = signed.rotation.old_identity.node_id

    with LocalTrustStore(
        tmp_path / "bounded-rotations.sqlite3",
        max_rotation_decisions=1,
    ) as store:
        store.reject_rotation(
            signed,
            decided_at=NOW + timedelta(minutes=1),
            reason="Rejected first.",
        )
        with pytest.raises(LocalTrustStoreError, match="limit"):
            store.accept_rotation(
                signed,
                decided_at=NOW + timedelta(minutes=6),
                reason="Would exceed the decision limit.",
            )
        assert store.resolve_node_id(old_node_id) == old_node_id
