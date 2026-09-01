import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runner_swarm.config import SwarmRuntimeConfig
from runner_swarm.node_manifest import (
    NodeEndpoint,
    NodeManifest,
    VersionedDeclaration,
    sign_node_manifest,
)
from runner_swarm.protocol import node_id_from_public_key, public_key_text
from runner_swarm.runtime import AttachedSwarmRuntime
from runner_swarm.signed_claim import (
    EvidenceReferenceV1,
    RunnerObservationV1,
    SignedClaimV1,
    SourceVersionV1,
)
from runner_swarm.transport import ClaimExchangeRequest, PeerClaimRejected
from runner_web.swarm_runtime import open_swarm_runtime


def _config(tmp_path: Path, **overrides: str) -> SwarmRuntimeConfig:
    return SwarmRuntimeConfig.from_env(
        {
            "SWARM_MODE": "attached",
            "SWARM_PUBLIC_URL": "https://node.example",
            "SWARM_KEY_PATH": str(tmp_path / "node.key"),
            "SWARM_PEER_STORE_PATH": str(tmp_path / "peer-claims.db"),
            "SWARM_PEER_RATE_LIMIT_PER_MINUTE": "4",
            **overrides,
        }
    )


def _peer_exchange(at: datetime, *, sequence: int = 0) -> ClaimExchangeRequest:
    key = Ed25519PrivateKey.from_private_bytes(b"p" * 32)
    public_key = public_key_text(key)
    node_id = node_id_from_public_key(public_key)
    topic = "markets/equities/us/runners"
    manifest = NodeManifest(
        node_id=node_id,
        public_key=public_key,
        issued_at=at,
        expires_at=at + timedelta(hours=1),
        software_name="peer-scanner",
        software_version="1.0.0",
        capabilities=(VersionedDeclaration(name="claims.publish", version="1.0.0"),),
        endpoints=(NodeEndpoint(transport="https", address="https://peer.example/swarm/v1"),),
        schema_versions=(VersionedDeclaration(name="rati.signed_claim", version="1.0.0"),),
        supported_topics=(topic,),
    )
    observed_at = at + timedelta(seconds=sequence)
    claim = RunnerObservationV1(
        issuer_node_id=node_id,
        issuer_public_key=public_key,
        issued_at=observed_at,
        expires_at=observed_at + timedelta(minutes=10),
        instrument="NASDAQ:PEN",
        observed_at=observed_at,
        scanner_version="market_risk_v3",
        schema_version="runner-v1",
        source_versions=(SourceVersionV1(family="market", source="example.bars", version="1"),),
        setup_score_milli=71_000 + sequence,
        rug_score_milli=20_000,
        rug_level="LOW",
        trade_state="WATCH",
        state_reason="Remote context only; local risk still decides.",
        evidence=(
            EvidenceReferenceV1(
                evidence_id="sha256:" + f"{sequence + 1:064x}",
                family="market",
                source="example.bars",
                observed_at=observed_at,
            ),
        ),
    )
    return ClaimExchangeRequest(
        topic=topic,
        peer_manifest=sign_node_manifest(manifest, key),
        signed_claim=SignedClaimV1.sign(claim, key),
    )


def test_live_transport_persists_peer_claims_and_filters_replays(tmp_path: Path) -> None:
    runtime = AttachedSwarmRuntime.open(_config(tmp_path))
    at = datetime.now(UTC).replace(microsecond=0)
    exchange = _peer_exchange(at)

    first = asyncio.run(runtime.transport.accept_claim(exchange))
    duplicate = asyncio.run(runtime.transport.accept_claim(exchange))

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert [item.signed_claim.claim_id for item in runtime.peer_store.current_claims(at=at)] == [
        exchange.signed_claim.claim_id
    ]
    runtime.close()


def test_live_transport_enforces_local_peer_bans(tmp_path: Path) -> None:
    runtime = AttachedSwarmRuntime.open(_config(tmp_path))
    at = datetime.now(UTC).replace(microsecond=0)
    exchange = _peer_exchange(at, sequence=1)
    runtime.peer_store.ban_peer(
        exchange.signed_claim.claim.issuer_node_id,
        reason="Local operator decision",
        at=at,
    )

    with pytest.raises(PeerClaimRejected, match="locally banned") as rejected:
        asyncio.run(runtime.transport.accept_claim(exchange))

    assert rejected.value.status_code == 403
    runtime.close()


def test_manifest_can_renew_without_changing_node_identity(tmp_path: Path) -> None:
    runtime = AttachedSwarmRuntime.open(_config(tmp_path))
    original = runtime.transport.signed_manifest
    replacement = runtime.renew_manifest(at=original.manifest.issued_at + timedelta(seconds=1))

    assert replacement.manifest.node_id == original.manifest.node_id
    assert replacement.content_id != original.content_id
    assert runtime.transport.signed_manifest == replacement
    runtime.close()


def test_solo_web_mode_starts_no_network_runtime() -> None:
    assert open_swarm_runtime({"SWARM_MODE": "solo"}) is None
