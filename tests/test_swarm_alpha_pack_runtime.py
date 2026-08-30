from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runner_swarm import runtime as swarm_runtime_module
from runner_swarm.alpha_pack import (
    MAX_SIGNED_PACK_BYTES,
    AlphaPack,
    AlphaPackError,
    EvidenceRequirements,
    LocalTrustPolicy,
    PeerReference,
    SignedAlphaPack,
    sign_alpha_pack,
)
from runner_swarm.config import SwarmRuntimeConfig
from runner_swarm.key_rotation import KeyRotationV1, RotationDecisionStatus, SignedKeyRotationV1
from runner_swarm.node_manifest import (
    NodeEndpoint,
    NodeManifest,
    VersionedDeclaration,
    sign_node_manifest,
)
from runner_swarm.protocol import NodeIdentity, node_id_from_public_key, public_key_text
from runner_swarm.remote_policy import RemoteClaimUse
from runner_swarm.reputation import OutcomeVerdict
from runner_swarm.runtime import (
    AttachedSwarmRuntime,
    BootstrapResult,
    apply_alpha_pack_policy,
    load_signed_alpha_pack,
)
from runner_swarm.signed_claim import (
    EvidenceReferenceV1,
    RunnerObservationV1,
    SignedClaimV1,
    SourceVersionV1,
)

NOW = datetime(2026, 8, 30, 18, tzinfo=UTC)
TOPIC = "markets/equities/us/runners"


def _identity(key: Ed25519PrivateKey, name: str) -> NodeIdentity:
    return NodeIdentity(
        node_id=node_id_from_public_key(key),
        public_key=public_key_text(key),
        display_name=name,
    )


def _peer(
    key: Ed25519PrivateKey,
    *,
    roles: tuple[str, ...] = ("approved", "bootstrap"),
    endpoints: tuple[str, ...] = ("https://bootstrap.example",),
) -> PeerReference:
    return PeerReference(
        **_identity(key, "Pack peer").model_dump(),
        roles=roles,
        endpoints=endpoints,
    )


def _signed_pack(
    owner_key: Ed25519PrivateKey,
    *,
    peers: tuple[PeerReference, ...] = (),
    topics: tuple[str, ...] = (TOPIC,),
    claim_versions: tuple[str, ...] = ("1",),
    schema_versions: tuple[str, ...] = ("runner-v1",),
    issued_at: datetime = NOW - timedelta(days=1),
    expires_at: datetime = NOW + timedelta(days=1),
    status: str = "active",
    revoked_at: datetime | None = None,
    trust_policy: LocalTrustPolicy | None = None,
    evidence_requirements: EvidenceRequirements | None = None,
) -> SignedAlphaPack:
    pack = AlphaPack(
        pack_id="runtime-pack",
        pack_version=1,
        name="Runtime pack",
        owner=_identity(owner_key, "Pack owner"),
        visibility="public",
        status=status,
        issued_at=issued_at,
        expires_at=expires_at,
        revoked_at=revoked_at,
        peers=peers,
        topics=topics,
        allowed_claim_versions=claim_versions,
        allowed_schema_versions=schema_versions,
        trust_policy=trust_policy or LocalTrustPolicy(),
        evidence_requirements=evidence_requirements or EvidenceRequirements(),
    )
    return sign_alpha_pack(pack, owner_key)


def _config(
    tmp_path: Path,
    *,
    alpha_pack_path: Path | None = None,
    bootstrap_urls: tuple[str, ...] = (),
    topics: tuple[str, ...] = (TOPIC,),
    schemas: tuple[str, ...] = ("runner-v1",),
    allow_private: bool = False,
) -> SwarmRuntimeConfig:
    return SwarmRuntimeConfig.from_env(
        {
            "SWARM_MODE": "attached",
            "SWARM_PUBLIC_URL": "https://node.example",
            "SWARM_KEY_PATH": str(tmp_path / "node.key"),
            "SWARM_PEER_STORE_PATH": str(tmp_path / "peer-claims.db"),
            "SWARM_LOCAL_TRUST_STORE_PATH": str(tmp_path / "local-trust.db"),
            "SWARM_ALPHA_PACK_PATH": str(alpha_pack_path or ""),
            "SWARM_BOOTSTRAP_URLS": ",".join(bootstrap_urls),
            "SWARM_TOPICS": ",".join(topics),
            "SWARM_CLAIM_SCHEMA_VERSIONS": ",".join(schemas),
            "SWARM_ALLOW_PRIVATE_BOOTSTRAP": str(allow_private).lower(),
        }
    )


def _write_pack(path: Path, signed_pack: SignedAlphaPack) -> None:
    path.write_bytes(signed_pack.to_wire_bytes())


def _observation(
    key: Ed25519PrivateKey,
    at: datetime,
    *,
    schema_version: str = "runner-v1",
) -> SignedClaimV1:
    evidence_id = "sha256:" + "a" * 64
    claim = RunnerObservationV1(
        issuer_node_id=node_id_from_public_key(key),
        issuer_public_key=public_key_text(key),
        issued_at=at,
        expires_at=at + timedelta(hours=1),
        instrument="NASDAQ:PEN",
        observed_at=at,
        scanner_version="market-risk-v3",
        schema_version=schema_version,
        source_versions=(SourceVersionV1(family="market", source="local-bars", version="1"),),
        setup_score_milli=78_000,
        rug_score_milli=18_000,
        rug_level="LOW",
        trade_state="WATCH",
        state_reason="Remote evidence remains context only.",
        evidence=(
            EvidenceReferenceV1(
                evidence_id=evidence_id,
                family="market",
                source="local-bars",
                observed_at=at,
            ),
        ),
    )
    return SignedClaimV1.sign(claim, key)


def _manifest(key: Ed25519PrivateKey, at: datetime, origin: str):
    return sign_node_manifest(
        NodeManifest(
            node_id=node_id_from_public_key(key),
            public_key=public_key_text(key),
            issued_at=at,
            expires_at=at + timedelta(hours=1),
            software_name="peer-scanner",
            software_version="1.0.0",
            capabilities=(
                VersionedDeclaration(name="claims.publish", version="1.0.0"),
                VersionedDeclaration(name="claims.receive", version="1.0.0"),
            ),
            endpoints=(NodeEndpoint(transport="https", address=f"{origin}/swarm/v1"),),
            schema_versions=(VersionedDeclaration(name="rati.signed_claim", version="1.0.0"),),
            supported_topics=(TOPIC,),
        ),
        key,
    )


def test_runtime_loader_reads_only_canonical_active_signed_packs(tmp_path: Path) -> None:
    owner_key = Ed25519PrivateKey.generate()
    signed_pack = _signed_pack(owner_key)
    pack_path = tmp_path / "alpha-pack.json"
    _write_pack(pack_path, signed_pack)

    assert load_signed_alpha_pack(pack_path, at=NOW) == signed_pack

    noncanonical_path = tmp_path / "noncanonical.json"
    noncanonical_path.write_text(
        json.dumps(json.loads(signed_pack.to_wire_bytes()), indent=2),
        encoding="utf-8",
    )
    with pytest.raises(AlphaPackError, match="canonical"):
        load_signed_alpha_pack(noncanonical_path, at=NOW)


def test_runtime_loader_rejects_links_and_oversized_files(tmp_path: Path) -> None:
    owner_key = Ed25519PrivateKey.generate()
    target = tmp_path / "target.json"
    _write_pack(target, _signed_pack(owner_key))
    link = tmp_path / "linked.json"
    link.symlink_to(target)

    with pytest.raises(AlphaPackError, match="regular file, not a link"):
        load_signed_alpha_pack(link, at=NOW)

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (MAX_SIGNED_PACK_BYTES + 1))
    with pytest.raises(AlphaPackError, match="safe input size"):
        load_signed_alpha_pack(oversized, at=NOW)


@pytest.mark.parametrize(
    "signed_pack",
    [
        _signed_pack(
            Ed25519PrivateKey.from_private_bytes(b"e" * 32),
            expires_at=NOW,
        ),
        _signed_pack(
            Ed25519PrivateKey.from_private_bytes(b"r" * 32),
            status="revoked",
            revoked_at=NOW - timedelta(hours=1),
        ),
    ],
)
def test_runtime_loader_rejects_inactive_pack(
    tmp_path: Path,
    signed_pack: SignedAlphaPack,
) -> None:
    pack_path = tmp_path / "inactive.json"
    _write_pack(pack_path, signed_pack)

    with pytest.raises(AlphaPackError, match="not active"):
        load_signed_alpha_pack(pack_path, at=NOW)


def test_pack_policy_restricts_versions_topics_and_collects_safe_https_seeds(
    tmp_path: Path,
) -> None:
    owner_key = Ed25519PrivateKey.generate()
    peer_key = Ed25519PrivateKey.generate()
    signed_pack = _signed_pack(
        owner_key,
        peers=(
            _peer(
                peer_key,
                endpoints=(
                    "/dns4/peer.example/tcp/443/wss",
                    "https://127.0.0.1",
                    "https://bootstrap.example",
                ),
            ),
        ),
    )
    config = _config(
        tmp_path,
        bootstrap_urls=("https://manual.example/",),
    )

    policy = apply_alpha_pack_policy(config, signed_pack)

    assert policy.effective_config.bootstrap_urls == (
        "https://manual.example",
        "https://bootstrap.example",
    )
    assert policy.bootstrap_peer_ids == (
        ("https://bootstrap.example", node_id_from_public_key(peer_key)),
    )
    assert policy.approved_peer_node_ids == frozenset({node_id_from_public_key(peer_key)})


@pytest.mark.parametrize(
    ("pack_updates", "config_updates", "message"),
    [
        ({"claim_versions": ("2",)}, {}, "protocol version"),
        (
            {"topics": ("markets/crypto/runners",)},
            {},
            "topics are not allowed",
        ),
        (
            {"schema_versions": ("runner-v2",)},
            {},
            "schemas are not allowed",
        ),
    ],
)
def test_pack_policy_fails_closed_on_local_compatibility_mismatch(
    tmp_path: Path,
    pack_updates: dict[str, object],
    config_updates: dict[str, object],
    message: str,
) -> None:
    owner_key = Ed25519PrivateKey.generate()
    signed_pack = _signed_pack(owner_key, **pack_updates)
    config = _config(tmp_path, **config_updates)

    with pytest.raises(AlphaPackError, match=message):
        apply_alpha_pack_policy(config, signed_pack)


def test_pack_policy_rejects_one_origin_bound_to_two_peer_identities(tmp_path: Path) -> None:
    owner_key = Ed25519PrivateKey.generate()
    signed_pack = _signed_pack(
        owner_key,
        peers=(
            _peer(Ed25519PrivateKey.generate()),
            _peer(Ed25519PrivateKey.generate()),
        ),
    )

    with pytest.raises(AlphaPackError, match="more than one peer identity"):
        apply_alpha_pack_policy(_config(tmp_path), signed_pack)


def test_runtime_rejects_bootstrap_manifest_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    owner_key = Ed25519PrivateKey.generate()
    expected_peer_key = Ed25519PrivateKey.generate()
    substituted_peer_key = Ed25519PrivateKey.generate()
    signed_pack = _signed_pack(
        owner_key,
        peers=(_peer(expected_peer_key),),
        issued_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=1),
    )
    pack_path = tmp_path / "runtime-pack.json"
    _write_pack(pack_path, signed_pack)
    runtime = AttachedSwarmRuntime.open(_config(tmp_path, alpha_pack_path=pack_path), at=now)
    monkeypatch.setattr(
        swarm_runtime_module,
        "fetch_signed_manifest",
        lambda *_args, **_kwargs: _manifest(
            substituted_peer_key,
            now,
            "https://bootstrap.example",
        ),
    )

    results = runtime.refresh_bootstraps(at=now)

    assert len(results) == 1
    assert results[0].connected is False
    assert results[0].peer_node_id is None
    assert "does not match the alpha pack peer" in (results[0].error or "")
    runtime.close()


def test_runtime_revalidates_pack_file_before_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    owner_key = Ed25519PrivateKey.generate()
    peer_key = Ed25519PrivateKey.generate()
    pack_path = tmp_path / "runtime-pack.json"
    active_pack = _signed_pack(
        owner_key,
        peers=(_peer(peer_key),),
        issued_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=1),
    )
    _write_pack(pack_path, active_pack)
    runtime = AttachedSwarmRuntime.open(_config(tmp_path, alpha_pack_path=pack_path), at=now)
    runtime.last_bootstrap_results = (
        BootstrapResult(
            origin="https://bootstrap.example",
            peer_node_id=node_id_from_public_key(peer_key),
            accepted_topics=(TOPIC,),
        ),
    )
    _write_pack(
        pack_path,
        _signed_pack(
            owner_key,
            peers=(_peer(peer_key),),
            issued_at=now - timedelta(hours=1),
            expires_at=now + timedelta(days=1),
            status="revoked",
            revoked_at=now,
        ),
    )

    with pytest.raises(AlphaPackError, match="not active"):
        runtime.refresh_bootstraps(at=now + timedelta(minutes=1))
    assert runtime.last_bootstrap_results == ()

    with pytest.raises(AlphaPackError, match="runtime is disabled"):
        runtime.renew_manifest(at=now + timedelta(minutes=2))

    _write_pack(pack_path, active_pack)
    monkeypatch.setattr(
        swarm_runtime_module,
        "fetch_signed_manifest",
        lambda *_args, **_kwargs: _manifest(
            peer_key,
            now + timedelta(minutes=3),
            "https://bootstrap.example",
        ),
    )
    monkeypatch.setattr(
        swarm_runtime_module,
        "negotiate_with_peer",
        lambda *_args, **_kwargs: SimpleNamespace(
            local_node_id=node_id_from_public_key(peer_key),
            accepted_topics=(TOPIC,),
        ),
    )
    recovered = runtime.refresh_bootstraps(at=now + timedelta(minutes=3))
    assert recovered[0].connected is True

    runtime.close()


def test_runtime_uses_pack_policy_and_persists_context_only_trust(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    owner_key = Ed25519PrivateKey.generate()
    peer_key = Ed25519PrivateKey.generate()
    signed_pack = _signed_pack(
        owner_key,
        peers=(_peer(peer_key, roles=("approved",), endpoints=()),),
        issued_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=1),
        trust_policy=LocalTrustPolicy(
            minimum_scored_outcomes=1,
            minimum_reputation_ppm=100_000,
            maximum_peer_claim_weight_ppm=200_000,
        ),
        evidence_requirements=EvidenceRequirements(
            minimum_receipts=1,
            minimum_independent_source_families=1,
        ),
    )
    pack_path = tmp_path / "runtime-pack.json"
    _write_pack(pack_path, signed_pack)
    config = _config(tmp_path, alpha_pack_path=pack_path)
    signed_claim = _observation(peer_key, now)
    evidence_id = signed_claim.claim.evidence[0].evidence_id

    runtime = AttachedSwarmRuntime.open(config, at=now)
    not_stored = runtime.assess_peer_claim(
        signed_claim,
        local_risk_allows=True,
        verified_evidence_ids=(evidence_id,),
        at=now,
    )
    assert "claim_not_active_in_peer_store" in not_stored.reasons

    runtime.peer_store.ingest(signed_claim, topic=TOPIC, received_at=now)
    runtime.record_peer_outcome(
        signed_claim,
        verdict=OutcomeVerdict.CONFIRMED,
        measured_at=now + timedelta(minutes=1),
        verified_claim_source_families=("market",),
        verification_source_families=("local-market",),
    )
    assessment = runtime.assess_peer_claim(
        signed_claim,
        local_risk_allows=True,
        verified_evidence_ids=(evidence_id,),
        at=now + timedelta(minutes=1),
    )

    assert assessment.use == RemoteClaimUse.CONTEXT_ONLY
    assert assessment.context_weight_ppm > 0
    assert assessment.can_execute_trade is False
    assert assessment.trade_command is None

    runtime.peer_store.ban_peer(
        signed_claim.claim.issuer_node_id,
        reason="Local operator block.",
        at=now + timedelta(minutes=2),
    )
    banned = runtime.assess_peer_claim(
        signed_claim,
        local_risk_allows=True,
        verified_evidence_ids=(evidence_id,),
        at=now + timedelta(minutes=2),
    )
    assert banned.use == RemoteClaimUse.REJECTED
    assert "peer_is_locally_banned" in banned.reasons

    runtime.peer_store.unban_peer(
        signed_claim.claim.issuer_node_id,
        at=now + timedelta(minutes=3),
    )
    runtime.peer_store.revoke_claim(
        signed_claim.claim_id,
        reason="Local claim revocation.",
        at=now + timedelta(minutes=3),
    )
    revoked = runtime.assess_peer_claim(
        signed_claim,
        local_risk_allows=True,
        verified_evidence_ids=(evidence_id,),
        at=now + timedelta(minutes=3),
    )
    assert revoked.use == RemoteClaimUse.REJECTED
    assert "claim_not_active_in_peer_store" in revoked.reasons

    runtime.peer_store.restore_claim(
        signed_claim.claim_id,
        at=now + timedelta(minutes=4),
    )
    replacement_claim = signed_claim.claim.model_copy(
        update={
            "issued_at": now + timedelta(minutes=4),
            "expires_at": now + timedelta(hours=1),
            "supersedes_claim_id": signed_claim.claim_id,
        }
    )
    replacement = SignedClaimV1.sign(replacement_claim, peer_key)
    runtime.peer_store.ingest(
        replacement,
        topic=TOPIC,
        received_at=now + timedelta(minutes=4),
    )
    superseded = runtime.assess_peer_claim(
        signed_claim,
        local_risk_allows=True,
        verified_evidence_ids=(evidence_id,),
        at=now + timedelta(minutes=4),
    )
    assert superseded.use == RemoteClaimUse.REJECTED
    assert "claim_not_active_in_peer_store" in superseded.reasons
    runtime.close()

    restored = AttachedSwarmRuntime.open(config, at=now + timedelta(minutes=2))
    assert restored.peer_reputation(node_id_from_public_key(peer_key)).confirmed == 1
    restored.close()


def test_runtime_rejects_context_from_peers_not_approved_by_pack(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    owner_key = Ed25519PrivateKey.generate()
    approved_key = Ed25519PrivateKey.generate()
    stranger_key = Ed25519PrivateKey.generate()
    signed_pack = _signed_pack(
        owner_key,
        peers=(_peer(approved_key, roles=("approved",), endpoints=()),),
        issued_at=now - timedelta(hours=1),
        expires_at=now + timedelta(days=1),
    )
    pack_path = tmp_path / "runtime-pack.json"
    _write_pack(pack_path, signed_pack)
    runtime = AttachedSwarmRuntime.open(_config(tmp_path, alpha_pack_path=pack_path), at=now)
    signed_claim = _observation(stranger_key, now)

    assessment = runtime.assess_peer_claim(
        signed_claim,
        local_risk_allows=True,
        verified_evidence_ids=(signed_claim.claim.evidence[0].evidence_id,),
        at=now,
    )

    assert assessment.use == RemoteClaimUse.REJECTED
    assert "peer_not_approved_by_alpha_pack" in assessment.reasons
    assert assessment.can_execute_trade is False
    runtime.close()


def test_runtime_key_rotation_decisions_are_durable_and_explicit(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    rotation = KeyRotationV1(
        old_identity=_identity(old_key, "Old peer"),
        new_identity=_identity(new_key, "New peer"),
        sequence=1,
        issued_at=now,
        effective_at=now + timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
        reason="Routine rotation.",
    )
    signed_rotation = SignedKeyRotationV1.sign(rotation, old_key, new_key)
    config = _config(tmp_path)

    runtime = AttachedSwarmRuntime.open(config, at=now)
    rejected = runtime.reject_peer_key_rotation(
        signed_rotation,
        decided_at=now,
        reason="Waiting for local confirmation.",
    )
    accepted = runtime.accept_peer_key_rotation(
        signed_rotation,
        decided_at=now + timedelta(minutes=2),
        reason="Confirmed through a separate local channel.",
    )
    assert rejected.status == RotationDecisionStatus.REJECTED
    assert accepted.status == RotationDecisionStatus.ACCEPTED
    assert runtime.resolve_peer_node_id(rotation.old_identity.node_id) == (
        rotation.new_identity.node_id
    )
    runtime.close()

    restored = AttachedSwarmRuntime.open(config, at=now + timedelta(minutes=3))
    assert restored.resolve_peer_node_id(rotation.old_identity.node_id) == (
        rotation.new_identity.node_id
    )
    revoked = restored.revoke_peer_key_rotation(
        signed_rotation.content_id,
        decided_at=now + timedelta(minutes=3),
        reason="Replacement key was compromised.",
    )
    assert revoked.status == RotationDecisionStatus.REVOKED
    assert restored.resolve_peer_node_id(rotation.old_identity.node_id) == (
        rotation.old_identity.node_id
    )
    restored.close()


def test_accepted_rotation_updates_alpha_pack_bootstrap_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    owner_key = Ed25519PrivateKey.generate()
    old_key = Ed25519PrivateKey.generate()
    new_key = Ed25519PrivateKey.generate()
    pack_path = tmp_path / "runtime-pack.json"
    _write_pack(
        pack_path,
        _signed_pack(
            owner_key,
            peers=(_peer(old_key),),
            issued_at=now - timedelta(hours=1),
            expires_at=now + timedelta(days=1),
        ),
    )
    runtime = AttachedSwarmRuntime.open(_config(tmp_path, alpha_pack_path=pack_path), at=now)
    rotation = KeyRotationV1(
        old_identity=_identity(old_key, "Old peer"),
        new_identity=_identity(new_key, "New peer"),
        sequence=1,
        issued_at=now,
        effective_at=now + timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
        reason="Routine rotation.",
    )
    runtime.accept_peer_key_rotation(
        SignedKeyRotationV1.sign(rotation, old_key, new_key),
        decided_at=now + timedelta(minutes=2),
        reason="Confirmed through a separate local channel.",
    )
    monkeypatch.setattr(
        swarm_runtime_module,
        "fetch_signed_manifest",
        lambda *_args, **_kwargs: _manifest(
            new_key,
            now + timedelta(minutes=2),
            "https://bootstrap.example",
        ),
    )
    expected_ids: list[str | None] = []

    def negotiate(*_args: object, **kwargs: object) -> SimpleNamespace:
        expected_ids.append(kwargs.get("expected_peer_node_id"))  # type: ignore[arg-type]
        return SimpleNamespace(
            local_node_id=node_id_from_public_key(new_key),
            accepted_topics=(TOPIC,),
        )

    monkeypatch.setattr(swarm_runtime_module, "negotiate_with_peer", negotiate)

    results = runtime.refresh_bootstraps(at=now + timedelta(minutes=2))

    assert results[0].connected is True
    assert results[0].peer_node_id == node_id_from_public_key(new_key)
    assert expected_ids == [node_id_from_public_key(new_key)]
    runtime.close()
