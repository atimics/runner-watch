"""Runtime assembly shared by solo, cloud, and alpha-pack trader nodes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter

from runner_swarm.config import SwarmRuntimeConfig
from runner_swarm.identity import load_or_create_node_key
from runner_swarm.node_manifest import (
    NodeEndpoint,
    NodeManifest,
    SignedNodeManifest,
    VersionedDeclaration,
    sign_node_manifest,
)
from runner_swarm.peer_store import IngestOutcome, PeerClaimStore, PeerStoreLimits
from runner_swarm.protocol import node_id_from_public_key, public_key_text
from runner_swarm.signed_claim import SignedClaimV1
from runner_swarm.transport import (
    ClaimExchangeReceipt,
    PeerClaimRejected,
    ReceivedPeerClaim,
    SwarmTransport,
    create_swarm_router,
    fetch_signed_manifest,
    negotiate_with_peer,
    post_claim_to_peer,
)


@dataclass(frozen=True, slots=True)
class SwarmNodeIdentity:
    """One stable local identity that can sign every swarm contract."""

    private_key: Ed25519PrivateKey
    public_key: str
    node_id: str

    @classmethod
    def load(cls, config: SwarmRuntimeConfig) -> SwarmNodeIdentity:
        private_key = load_or_create_node_key(config.key_path)
        public_key = public_key_text(private_key)
        return cls(
            private_key=private_key,
            public_key=public_key,
            node_id=node_id_from_public_key(public_key),
        )


def build_signed_runtime_manifest(
    config: SwarmRuntimeConfig,
    identity: SwarmNodeIdentity,
    *,
    at: datetime | None = None,
) -> SignedNodeManifest:
    """Build the short-lived public discovery record for an attached node."""

    if config.public_url is None:
        raise ValueError("SWARM_PUBLIC_URL is required to publish a swarm manifest")
    issued_at = (at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    manifest = NodeManifest(
        node_id=identity.node_id,
        public_key=identity.public_key,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=config.manifest_ttl_seconds),
        software_name="runner-watch",
        software_version=config.software_version,
        capabilities=(
            VersionedDeclaration(name="claims.publish", version="1.0.0"),
            VersionedDeclaration(name="claims.receive", version="1.0.0"),
            VersionedDeclaration(name="discovery.manifest", version="1.0.0"),
        ),
        endpoints=(NodeEndpoint(transport="https", address=f"{config.public_url}/swarm/v1"),),
        schema_versions=(
            VersionedDeclaration(name="rati.alpha_pack", version="1.0.0"),
            VersionedDeclaration(name="rati.signed_claim", version="1.0.0"),
        ),
        supported_topics=config.topics,
    )
    return sign_node_manifest(manifest, identity.private_key)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """One local view of a configured bootstrap attempt."""

    origin: str
    peer_node_id: str | None
    accepted_topics: tuple[str, ...]
    error: str | None = None

    @property
    def connected(self) -> bool:
        return self.peer_node_id is not None and self.error is None


class AttachedSwarmRuntime:
    """Assemble discovery, exchange, and the isolated peer-claim trust boundary."""

    def __init__(
        self,
        config: SwarmRuntimeConfig,
        identity: SwarmNodeIdentity,
        signed_manifest: SignedNodeManifest,
        peer_store: PeerClaimStore,
    ) -> None:
        if not config.attached:
            raise ValueError("AttachedSwarmRuntime requires SWARM_MODE=attached")
        self.config = config
        self.identity = identity
        self.peer_store = peer_store
        self.router: APIRouter = create_swarm_router(
            signed_manifest,
            receive_claim=self.receive_peer_claim,
            accepted_claim_schema_versions=frozenset({"runner-v1"}),
        )
        self.transport: SwarmTransport = self.router.swarm_transport  # type: ignore[attr-defined]
        self.last_bootstrap_results: tuple[BootstrapResult, ...] = ()

    @classmethod
    def open(cls, config: SwarmRuntimeConfig) -> AttachedSwarmRuntime:
        identity = SwarmNodeIdentity.load(config)
        signed_manifest = build_signed_runtime_manifest(config, identity)
        peer_store = PeerClaimStore(
            config.peer_store_path,
            limits=PeerStoreLimits(
                claims_per_window=config.peer_rate_limit_per_minute,
            ),
        )
        return cls(config, identity, signed_manifest, peer_store)

    def receive_peer_claim(self, received: ReceivedPeerClaim) -> bool:
        """Persist a verified claim as peer context, never as provider evidence."""

        result = self.peer_store.ingest(
            received.signed_claim,
            topic=received.topic,
            received_at=received.received_at,
        )
        if result.outcome == IngestOutcome.BANNED:
            raise PeerClaimRejected("Peer is locally banned", status_code=403)
        if result.outcome == IngestOutcome.RATE_LIMITED:
            raise PeerClaimRejected("Peer claim rate limit exceeded", status_code=429)
        return result.outcome == IngestOutcome.ACCEPTED

    def renew_manifest(self, *, at: datetime | None = None) -> SignedNodeManifest:
        signed_manifest = build_signed_runtime_manifest(
            self.config,
            self.identity,
            at=at,
        )
        self.transport.replace_local_manifest(signed_manifest)
        return signed_manifest

    def refresh_bootstraps(self, *, at: datetime | None = None) -> tuple[BootstrapResult, ...]:
        """Discover and negotiate every configured seed without trusting membership."""

        results: list[BootstrapResult] = []
        for origin in self.config.bootstrap_urls:
            try:
                peer_manifest = fetch_signed_manifest(
                    origin,
                    at=at,
                    allow_private_addresses=self.config.allow_private_bootstrap,
                )
                response = negotiate_with_peer(
                    origin,
                    self.transport.signed_manifest,
                    self.config.topics,
                    expected_peer_node_id=peer_manifest.manifest.node_id,
                    at=at,
                    allow_private_addresses=self.config.allow_private_bootstrap,
                )
                results.append(
                    BootstrapResult(
                        origin=origin,
                        peer_node_id=response.local_node_id,
                        accepted_topics=response.accepted_topics,
                    )
                )
            except (OSError, ValueError) as error:
                results.append(
                    BootstrapResult(
                        origin=origin,
                        peer_node_id=None,
                        accepted_topics=(),
                        error=f"{type(error).__name__}: {error}"[:280],
                    )
                )
        self.last_bootstrap_results = tuple(results)
        return self.last_bootstrap_results

    def send_claim(
        self,
        peer_origin: str,
        signed_claim: SignedClaimV1,
        topic: str,
        *,
        expected_peer_node_id: str | None = None,
        at: datetime | None = None,
    ) -> ClaimExchangeReceipt:
        return post_claim_to_peer(
            peer_origin,
            self.transport.signed_manifest,
            signed_claim,
            topic,
            expected_peer_node_id=expected_peer_node_id,
            at=at,
            allow_private_addresses=self.config.allow_private_bootstrap,
        )

    def close(self) -> None:
        self.peer_store.close()
