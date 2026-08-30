# RATi AlphaPack v1

An AlphaPack is signed swarm membership and policy configuration. It is never executable code,
an order, or proof that a peer is trustworthy. Every trader keeps its own risk gate and may apply
stricter local trust rules than a pack recommends.

## Wire format

The wire envelope is `SignedAlphaPack`:

- `pack`: the signed AlphaPack content.
- `content_id`: `sha256:` followed by the SHA-256 digest of the canonical pack bytes.
- `signature_algorithm`: `ed25519`.
- `signature`: URL-safe base64 without padding.

The shared swarm canonical JSON profile is compact UTF-8 with sorted keys, NFC-normalized text,
UTC timestamps with six fractional digits and `Z`, portable safe-range integers, and no floating
point values. The signature input is `RATI-SWARM\0rati.alpha_pack\01\0` followed by the canonical
pack bytes. The prefix prevents a valid pack signature from being reused as another RATi message
type or protocol version.

Ed25519 public keys and signatures use canonical URL-safe base64 without padding. A node ID is
`rati-node:` plus the lowercase SHA-256 hex digest of its 32-byte public key.

## Pack content

Identity and lifecycle fields include the fixed message and protocol versions, stable `pack_id`,
increasing `pack_version`, owner identity, issue and expiry times, active or revoked status,
optional revocation time, and an optional previous content ID that this version supersedes. A new
version should name the prior signed artifact in `supersedes_content_id`; it does not mutate it.
One signed version can remain active for at most 366 days before it must be renewed.

The membership section contains peer identities and their roles:

- `bootstrap` means the peer can help establish network connectivity.
- `approved` means the owner included the peer in this pack.
- A peer can have both roles, but its node ID appears only once.

Neither role grants trust. `LocalTrustPolicy.membership_grants_trust` is fixed to `false`, new-peer
weight is tightly limited, and `require_local_risk_gate` is fixed to `true`. Trust weights are
encoded as integer parts per million. Outcome history and local policy decide how much influence
a claim receives.

Topics and claim/schema version allowlists define compatibility. Evidence policy sets minimum
receipt and independent-source counts, digest requirements, maximum evidence age, and permanently
disables redistribution of licensed raw data through the pack.

Private packs require `PrivatePackEncryption`. This holds only a public cipher-suite name, group
key ID, recipient key IDs, and an optional encrypted-payload locator. Secret keys are rejected as
unknown fields and must remain in the node's secure key store. A public pack cannot carry this
private routing metadata.

## Python use

```python
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runner_swarm.alpha_pack import (
    AlphaPack,
    NodeIdentity,
    sign_alpha_pack,
)
from runner_swarm.protocol import node_id_from_public_key, public_key_text

key = Ed25519PrivateKey.generate()
public_key = public_key_text(key)
owner = NodeIdentity(
    node_id=node_id_from_public_key(public_key),
    public_key=public_key,
)
now = datetime.now(UTC)
pack = AlphaPack(
    pack_id="biotech-catalysts",
    pack_version=1,
    name="Biotech Catalysts",
    owner=owner,
    visibility="public",
    issued_at=now,
    expires_at=now + timedelta(days=30),
    topics=["market.biotech", "sec.filings"],
    allowed_claim_versions=["1"],
    allowed_schema_versions=["runner-snapshot/1"],
)
signed = sign_alpha_pack(pack, key)
wire_bytes = signed.to_wire_bytes()

# A receiver parses first, then verifies identity, signature, and activity.
received = type(signed).from_wire_bytes(wire_bytes)
received.verify()
```

Parsing alone does not verify a signature. Call `verify()` before accepting configuration. A node
that needs archived or revoked packs for audit can call `verify(require_active=False)` while still
checking the content ID and signature.

## Safety limits

The implementation limits a pack to 256 KiB of canonical content, 128 peers, 256 topics, eight
endpoints per peer, 32 entries in each version allowlist, and 256 recipient key IDs. Receiving code
also limits envelope input size before parsing. These limits keep discovery inputs cheap to reject
and reduce memory or signature-verification abuse.
