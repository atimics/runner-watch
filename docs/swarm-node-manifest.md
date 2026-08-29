# RATi swarm NodeManifest v1

`NodeManifest` is the small, signed discovery record for one RATi trader node. The
same format can be published at `/.well-known/rati-swarm.json`, returned by a
bootstrap service, or exchanged directly between peers.

The implementation is in `src/runner_swarm/node_manifest.py`. Version 1 uses frozen
Pydantic models with strict input handling and rejects unknown fields.

## Wire format

The outer `SignedNodeManifest` contains:

- `manifest`: the discovery payload described below.
- `content_hash`: `sha256:` followed by the lowercase SHA-256 digest of the canonical
  manifest bytes.
- `signature`: the Ed25519 signature as URL-safe base64 without padding.

The manifest contains:

- A fixed `message_type` of `rati.node-manifest` and `protocol_version` of `1`.
- `node_id`, calculated as `sha256:<hex>` over the raw 32-byte Ed25519 public key.
- The public key as URL-safe base64 without padding.
- Whole-second UTC `issued_at` and `expires_at` timestamps.
- The lowercase software name and its semantic version.
- Versioned capability and supported payload-schema declarations.
- Public HTTPS, WSS, or libp2p endpoints.
- Lowercase, namespaced topics that the node can exchange.

Set-like fields are sorted before serialization. Canonical JSON is UTF-8, has sorted
object keys, and uses compact separators with no extra whitespace. A manifest cannot
exceed 16 KiB or remain valid for more than seven days.

The signature input is:

```text
"RATI-SWARM\\0node-manifest\\0v1\\0" || canonical_manifest_json
```

The fixed prefix prevents a valid NodeManifest signature from being reused as a
signature for another RATi message type or protocol version.

## Minimal use

```python
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runner_swarm.node_manifest import (
    NodeEndpoint,
    NodeManifest,
    VersionedDeclaration,
    node_id_from_public_key,
    public_key_base64,
    sign_node_manifest,
    verify_signed_node_manifest,
)

private_key = Ed25519PrivateKey.generate()
public_key = public_key_base64(private_key.public_key())
now = datetime.now(UTC).replace(microsecond=0)

manifest = NodeManifest(
    node_id=node_id_from_public_key(public_key),
    public_key=public_key,
    issued_at=now,
    expires_at=now + timedelta(hours=24),
    software_name="runner-watch",
    software_version="0.1.0",
    capabilities=(VersionedDeclaration(name="claims.publish", version="1.0.0"),),
    endpoints=(
        NodeEndpoint(transport="https", address="https://node.example/swarm"),
    ),
    schema_versions=(
        VersionedDeclaration(name="rati.signed-claim", version="1.0.0"),
    ),
    supported_topics=("markets/equities/us/runners",),
)

wire_record = sign_node_manifest(manifest, private_key)
verified_manifest = verify_signed_node_manifest(wire_record)
```

Persist the private key in the operating system's secret store. Do not include it in
the manifest, logs, repository, invite links, or alpha packs.

## Verification rules

A receiver must parse the strict envelope and then call
`verify_signed_node_manifest`. Verification rejects:

- A content hash that does not match the canonical manifest.
- A signature that is not valid for the advertised public key and v1 signature domain.
- A node ID that was not derived from the advertised public key.
- An expired manifest or one issued more than five minutes in the future.
- Invalid, duplicate, oversized, or unknown declarations.

Expiry is exclusive: a manifest is invalid when `expires_at` equals the verification
time. Nodes should refresh manifests well before expiry. A verified manifest proves
control of a node key; it does not prove that the operator is honest or that an
endpoint is safe.

## Security and privacy

- Treat endpoints as untrusted input. Apply normal outbound request controls, DNS
  rebinding protection, connection limits, timeouts, and response-size limits before
  dialing them.
- Do not publish LAN addresses, user IDs, account names, machine names, API keys,
  bearer tokens, query credentials, portfolio holdings, or data-source credentials.
  The URL validator rejects credentials, queries, and fragments but cannot recognize
  every sensitive path or hostname.
- Publish only addresses that are intentionally reachable. Local mDNS discovery
  should use a separate ephemeral advertisement and should not copy private addresses
  into the public manifest.
- Rotate a compromised node key. Rotation creates a new `node_id`; trust transfer must
  happen through a separate, explicit signed process.
- A signature supplies integrity and node continuity, not reputation. Keep trust and
  execution decisions local, and never interpret a capability as authority to trade.
- Topics and capabilities reveal a node's interests. Operators that need privacy
  should advertise broad categories or exchange a smaller private manifest directly.
- Serve well-known manifests over HTTPS with conservative caching no later than their
  expiry. Consumers must still verify the Ed25519 signature and must not rely only on
  TLS or a bootstrap directory.
