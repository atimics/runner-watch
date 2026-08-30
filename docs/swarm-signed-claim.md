# SignedClaim v1

`SignedClaimV1` is the first RATi peer-intelligence message. It carries a scanner's statement to
another trader. It never carries an order or an instruction to trade.

Peers must treat every remote claim as untrusted input. A valid signature proves which node made
the statement and that its content did not change. It does not prove that the statement is true,
well sourced, or suitable for a trade. The receiving trader keeps its own risk gate and trust
score.

The executable models and signing code live in `runner_swarm.signed_claim`.

## Envelope

A wire message has three fields:

```json
{
  "claim": { "kind": "runner_observation", "...": "..." },
  "claim_id": "sha256:<64 lowercase hex characters>",
  "signature": "<Ed25519 signature as unpadded base64url>"
}
```

The claim includes the issuer's `rati-node:<sha256>` ID and raw 32-byte Ed25519 public key as
unpadded, URL-safe base64. The ID must match the key, so a receiver can verify it without an
account registry and connect it to the same identity in a NodeManifest or AlphaPack.

The content ID is `sha256:` followed by the SHA-256 digest of the canonical claim bytes. It is the
identity of this exact signed statement. It is not an identity for a symbol, setup, or evidence
document.

## Canonical bytes and signatures

Canonical JSON is UTF-8 with:

- keys sorted by Unicode code point
- no optional whitespace
- non-ASCII text kept as UTF-8
- UTC timestamps written with six fractional digits and a trailing `Z`
- portable safe-range integers only; floating-point values are forbidden

An Ed25519 signature covers:

```text
"RATI-SWARM" || 0x00 || "rati.signed_claim" || 0x00 || "1" || 0x00
|| canonical_claim_json
```

The prefix separates these signatures from other RATi message types and future versions. The wire
reader rejects JSON that is not already canonical. This gives every accepted message one byte
representation.

## Runner observations

`RunnerObservationV1` contains:

- the instrument and observation, issue, and expiry times
- scanner and schema versions
- the versions of source adapters used
- setup score and rug score in thousandths, plus rug level
- trade state and its plain-language reason
- bounded signal labels and risk vetoes
- one to 32 evidence references
- an optional claim ID that this observation supersedes

Trade states match the local scanner: `WATCH`, `ARMED`, `TRIGGERED`, `MANAGE`, `AVOID`, and
`EXIT`. Risk vetoes say whether a warning is advisory or hard and may point to evidence included in
the same claim. They are reported peer state, not commands. A receiving node must not let a remote
`TRIGGERED` state bypass its local risk rules.

Evidence references contain a family, source name, observation time, optional locator, and a
SHA-256 content reference. There is deliberately no raw evidence payload. Peers must not
redistribute licensed raw market data, news, research, or other provider material unless their
licence clearly permits it. A hash does not itself grant redistribution rights.

Remote evidence references and source versions must remain separate from trusted
`ProviderProvenance` and canonical provider records. A peer claim may point a node toward evidence;
it must never be silently promoted into a first-party provider record.

## Expiry, replacement, and retraction

All statements expire. Runner observations may live for at most 24 hours. Retractions may live for
at most seven days. Receivers should normally reject expired claims, while retaining them in a
bounded audit store when policy allows.

An observation may set `supersedes_claim_id`. It replaces the named claim only when both claims:

1. have valid signatures,
2. use the same issuer public key, and
3. have an exact content-ID match.

A `RetractionV1` names one target claim and gives a reason. It has the same three checks. A node
cannot retract or supersede another node's statement. A retraction does not erase audit history;
it changes how an active consumer views the target while the retraction is current. Expired claims
already stop being current without a retraction.

## Bounds and receiver rules

The model caps content at 24 KiB and the signed wire message at 32 KiB. It also caps source
versions, evidence references, signals, vetoes, text lengths, locators, and relation counts. These
limits must be checked before a message enters gossip or durable storage.

A receiver should apply checks in this order:

1. Reject a message over the wire-size limit.
2. Parse with unknown fields forbidden and confirm canonical wire bytes.
3. Recompute the content ID and verify the domain-separated Ed25519 signature.
4. Reject claims issued too far in the future or already expired.
5. Store the message as an untrusted peer claim, separate from provider data.
6. Apply local peer reputation, evidence policy, duplicate-source detection, and risk rules.

Signature validity should never be used as a reputation score. Sybil resistance, replay storage,
rate limits, peer scoring, key rotation, and revocation are discovery/transport policy and are not
solved by SignedClaim v1.
