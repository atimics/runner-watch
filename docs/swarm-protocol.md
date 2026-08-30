# RATi swarm protocol v1

RATi swarm v1 defines three signed objects that let the same trader run alone, attach to the
default cloud swarm, or join an alpha pack. The contract modules do not make remote claims trusted.
The optional HTTPS transport in `docs/swarm-transport.md` publishes and exchanges them while
preserving that boundary.

## Object chain

1. `NodeManifest` says how to reach a node and which versions, schemas, capabilities, and topics
   it supports.
2. `SignedClaimV1` carries one short-lived scanner observation or a retraction. It is evidence for
   a receiving trader to judge, never a command.
3. `AlphaPack` names peers, bootstrap addresses, topics, compatibility rules, evidence minimums,
   and local trust recommendations. Membership never grants trust.

The contracts use one Ed25519 identity. The node ID is `rati-node:` plus the lowercase SHA-256
digest of the raw 32-byte public key. The same node ID and public key bind a manifest, the issuer
of a claim, an alpha-pack owner, and any peer entry.

## Common wire profile

Every object uses protocol version `1`, URL-safe base64 without padding, SHA-256 content IDs, and
the canonical JSON implementation in `runner_swarm.protocol`.

Canonical JSON is compact UTF-8 with sorted object keys, NFC-normalized text, UTC timestamps with
six fractional digits and `Z`, and integers limited to the portable JSON safe-integer range.
Floating-point values are forbidden in signed payloads. Scores use thousandths and trust weights
use parts per million, matching RATi's existing preference for replayable integer artifacts.

The common signature input is:

```text
"RATI-SWARM\0" || message_type || "\0" || protocol_version || "\0" || canonical_payload
```

The message type gives each contract a separate signing domain. A valid signature for one object
cannot be reused for another.

Receivers must reject oversized input before parsing, reject unknown fields, require canonical
wire bytes, recompute content IDs, verify signatures, and check issue and expiry times before
using an object.

## Trust boundary

A signature proves control of a node key and integrity of the signed bytes. It does not prove
that a scanner is honest, independent, timely, licensed to share data, or profitable.

Remote claims stay in a separate peer-claim store. They must not be inserted into trusted market
bars, provider provenance, SEC events, or other first-party evidence tables. The receiving trader
grades peers from locally observed outcomes, discounts repeated source families, and always keeps
its own risk vetoes authoritative.

Private alpha-pack metadata contains key identifiers and encrypted-payload routing only. Secret
keys never appear in a signed pack. The runtime now has local key-rotation decisions, peer bans and
revocations, replay storage, and rate limits. Encrypting private-pack content, Sybil resistance,
NAT traversal, rendezvous, and gossip remain future work.

## Versioning

Protocol version `1` fixes the envelope and signing rules. Named capability and schema declarations
use semantic versions such as `1.0.0`. Any change that alters canonical bytes, identity derivation,
signature domains, or field meaning requires a new protocol version and a separate verifier.

The detailed contracts are in:

- `docs/swarm-node-manifest.md`
- `docs/swarm-signed-claim.md`
- `docs/swarm-alpha-pack.md`
- `docs/swarm-transport.md`
