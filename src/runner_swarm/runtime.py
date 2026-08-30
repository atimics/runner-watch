"""Runtime assembly shared by solo, cloud, and alpha-pack trader nodes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runner_swarm.config import SwarmRuntimeConfig
from runner_swarm.identity import load_or_create_node_key
from runner_swarm.node_manifest import (
    NodeEndpoint,
    NodeManifest,
    SignedNodeManifest,
    VersionedDeclaration,
    sign_node_manifest,
)
from runner_swarm.protocol import node_id_from_public_key, public_key_text


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
        endpoints=(
            NodeEndpoint(transport="https", address=f"{config.public_url}/swarm/v1"),
        ),
        schema_versions=(
            VersionedDeclaration(name="rati.alpha_pack", version="1.0.0"),
            VersionedDeclaration(name="rati.signed_claim", version="1.0.0"),
        ),
        supported_topics=config.topics,
    )
    return sign_node_manifest(manifest, identity.private_key)
