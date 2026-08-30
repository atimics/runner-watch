# RATi swarm local trust and key rotation

The swarm treats every remote scanner claim as untrusted input. A valid signature identifies who
made a claim. It does not make that peer accurate, independent, or allowed to trade.

The local trust runtime has three parts:

- `runner_swarm.reputation` records outcomes measured by this node and calculates peer reputation.
- `runner_swarm.remote_policy` decides whether a current remote observation may be supporting
  context.
- `runner_swarm.key_rotation` proves continuity between two keys, while leaving the trust-transfer
  decision with the local operator.
- `runner_swarm.local_trust_store` keeps outcomes and rotation decisions in an isolated SQLite
  database so they survive restarts.

These modules do not use networking and never write provider data. The SQLite tables use the
`swarm_local_` prefix and should live in a dedicated local trust database file.

## Outcome records and reputation

`LocalOutcomeLedger.record` accepts a signed runner observation, verifies it, and derives the claim
ID, peer identity, and claimed source families from the signed content. The caller supplies the
outcome measured by the local node:

- `confirmed`: the stated setup was confirmed by the local measurement rule.
- `refuted`: the local measurement rule contradicted it.
- `inconclusive`: the result could not be graded and does not affect reliability.

The record also names the local source families used for verification. One claim can have only one
local result. It separately takes the claim source families whose evidence was actually verified;
they must be a subset of the signed claim. Repeating the exact record is safe; replacing it with a
different result is rejected.

Reputation uses integer parts per million. Its default reliability estimate starts with one
confirmed and three refuted prior outcomes. A peer with no history therefore has zero reputation
and zero influence. Resolved outcomes use this formula:

```text
reliability = (confirmed + 1) / (confirmed + refuted + 4)
```

Claims that repeatedly use the same evidence family are correlated, so they do not receive the
same weight as independent evidence. The source-diversity multiplier uses the inverse Simpson
concentration of the claimed families and reaches full weight at three evenly represented families
by default. One repeated family receives about one third of the reliability score.

A peer is eligible only after the local minimum outcome count and reputation threshold are both
met. Its influence is capped by the local maximum. Exact results do not depend on record order.

```python
from runner_swarm.reputation import LocalOutcomeLedger, OutcomeVerdict

ledger = LocalOutcomeLedger()
ledger.record(
    signed_claim,
    verdict=OutcomeVerdict.CONFIRMED,
    measured_at=measured_at,
    verified_claim_source_families=("market",),
    verification_source_families=("market", "regulatory"),
)
reputation = ledger.score(signed_claim.claim.issuer_node_id)
```

For normal runtime use, `LocalTrustStore.record_outcome` has the same measurement inputs and writes
the immutable record to SQLite. `LocalTrustStore.score(peer_node_id)` loads the stored records and
uses the same pure scoring function. Reopening the database produces the same score. If the local
operator accepted a key rotation, scoring the replacement identity includes outcomes from its
accepted predecessors. Rejecting or revoking that mapping immediately stops the transfer.

`AttachedSwarmRuntime` exposes this boundary through `record_peer_outcome`, `peer_reputation`, and
`assess_peer_claim`. It also exposes explicit `accept_peer_key_rotation`,
`reject_peer_key_rotation`, `revoke_peer_key_rotation`, and `resolve_peer_node_id` methods. These
delegate to the dedicated database selected by `SWARM_LOCAL_TRUST_STORE_PATH`; they do not write to
the trader's provider-data tables. An assessment result remains hard-coded as non-executable.

Outcome storage is bounded by both a per-peer and total record limit. The default limits are 10,000
outcomes per peer and 100,000 total. When a limit is exceeded, the oldest measurement time and claim
ID are removed first, making retention deterministic. Rotation history has a separate 10,000
decision limit. It fails closed at that limit instead of deleting a continuity decision; an
operator can archive the database and start a reviewed replacement.

Outcome definitions must be fixed locally before grading. Do not let a peer provide its own verdict,
rewrite old outcomes, or count several checks of the same claim as separate results.

## Remote claim execution gate

`assess_remote_claim` verifies the signed claim and applies the local trust policy, evidence
minimums, outcome count, reputation threshold, source-family count, evidence age, peer-weight cap,
and local risk gate. The caller must provide the evidence IDs it verified locally. Evidence merely
listed by the remote peer does not satisfy the gate.

An eligible result is `context_only`. The returned model hard-codes `can_execute_trade` to `False`
and `trade_command` to `None`. It does not copy the peer's `trade_state`. A remote `TRIGGERED`,
`EXIT`, or any future state is evidence for local analysis, never an execution instruction.

The assessment is rejected when the local risk gate blocks it, even if the peer has perfect local
history. Retractions are handled by claim lifecycle logic and are not trade context. A remote hard
risk veto also prevents the observation from becoming positive context.

## Key rotation

Rotating an Ed25519 key creates a new node ID. `KeyRotationV1` names the old and new identities, a
sequence number, issue/effective/expiry times, and a reason. `SignedKeyRotationV1` requires two
domain-separated signatures over the same canonical content:

1. the old key authorizes the move; and
2. the new key proves that the operator controls the replacement.

The proof is valid for at most 30 days. Both signatures, the content ID, time window, and identity
bindings are verified. The proof does not transfer trust on its own.

`LocalKeyRotationRegistry` makes that transfer explicit:

- `reject` records a local refusal and leaves the old identity unchanged.
- `accept` activates an old-to-new mapping no earlier than `effective_at`.
- `resolve` follows an accepted chain to the current identity.
- `revoke` disables an accepted mapping locally.

The registry rejects active forks, identity cycles, and older sequences after a newer local
decision. It keeps decision history so an operator can audit reject, accept, and revoke actions.
Revocation does not broadcast or erase the signed proof; transport code may distribute a separate
revocation notice later.

The SQLite store writes each signed rotation artifact beside its local decision. On every load it
checks canonical bytes, re-verifies both signatures, and replays accept, reject, and revoke history
in order. Rotation writes use an immediate SQLite transaction so two local processes cannot accept
different forks at the same time. A failed or over-limit write is rolled back.

## Safe integration order

1. Parse and verify the signed claim.
2. Keep it in an untrusted peer-claim store, separate from first-party provider data.
3. Resolve key continuity only through locally accepted, non-revoked rotation decisions.
4. Calculate reputation only from immutable local outcome records.
5. Apply the remote context gate and the local risk gate.
6. Let the existing local strategy and execution controls make every trade decision.
