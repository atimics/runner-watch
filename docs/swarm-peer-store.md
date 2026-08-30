# Peer claim safety store

`PeerClaimStore` is the local trust boundary between swarm transport and the trader runtime. It
accepts `SignedClaimV1` messages, verifies them, applies local admission policy, and keeps a bounded
view of current peer observations.

The default database is `data/swarm/peer_claims.sqlite3`. It is intentionally a separate SQLite
file. Do not point it at the runner database, and do not copy its rows into provider evidence,
market bars, filings, or other canonical data tables. A peer statement remains untrusted even when
its signature is valid.

## Runtime use

```python
from runner_swarm.peer_store import PeerClaimStore

with PeerClaimStore() as peers:
    result = peers.ingest_wire(body, topic="equities/us/runners")
    if result.accepted:
        observations = peers.current_claims(topic="equities/us/runners")
```

`ingest_wire` rejects non-canonical wire messages, bad signatures, claims from the future, and
expired claims before opening the database. An accepted result means only that the message passed
the transport safety boundary. It must still pass local evidence, reputation, position, and risk
rules. Remote trade states never place orders.

## Safety behavior

- Claim IDs are unique across every topic. Receiving the same signed content again produces a
  `duplicate` result and an audit event, without writing a second claim.
- Rate limits use fixed windows per verified node ID and normalized topic. A peer can fill only its
  own topic bucket.
- A local peer ban blocks new claims and hides that peer's stored observations from the current
  view. Bans may be permanent or expire at a stated UTC time.
- A local claim revocation hides one exact claim. Both peer bans and claim revocations are local
  policy; they are never sent as if the peer signed them.
- Supersession is permanent for the named older observation. Only a later observation from the
  same key and instrument can supersede it.
- A peer retraction affects only an exact claim from the same key, and only while that retraction is
  current. The audit record stays in storage after the target leaves the current view.
- Claim state is refreshed at read, ingest, and prune time, so an expired observation is never
  returned as current.

## Bounds and pruning

`PeerStoreLimits` controls claims per rate window, maximum stored claims, maximum audit events,
maximum local control records, and inactive retention. Pruning removes old inactive claims first.
If the hard claim cap is still exceeded, it removes the oldest remaining records. Rate-window rows,
expired bans, and audit rows are bounded as well. The store rejects new local controls when their
cap is full instead of silently dropping an existing ban or revocation.

Applications should call `prune()` on a normal maintenance cycle even when no claims are arriving.
Pruning is a local capacity decision, not a statement about peer reputation.

The store retains the original canonical wire bytes for audit and re-verification. It stores only
the peer claim's evidence references; it does not fetch, license, promote, or certify the referenced
provider material.
