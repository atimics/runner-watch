# Running the RATi swarm

The swarm lets one RATi scanner exchange short-lived observations with other chosen scanners. The
same application code can stay private and independent or attach to peers. Remote observations are
extra context. They are never trade commands.

## Choose a mode

`SWARM_MODE=solo` is the default. In solo mode the app does not open the swarm routes, discover
peers, receive peer claims, or publish scan claims. Normal local and hosted scanner work continues.

`SWARM_MODE=attached` starts the HTTPS swarm runtime. An attached web process serves:

- `GET /.well-known/rati-swarm.json` for its signed node manifest;
- `POST /swarm/v1/negotiate` for topic negotiation; and
- `POST /swarm/v1/claims` for bounded signed-claim delivery.

The worker also refreshes configured seeds. Publishing local scan results is a separate choice and
stays off until `SWARM_PUBLISH_SCANS=true` is set.

An attached node needs a public HTTPS origin:

```text
SWARM_MODE=attached
SWARM_PUBLIC_URL=https://scanner.example
SWARM_BOOTSTRAP_URLS=https://seed.example
```

The public URL is an origin, without a path, query, credentials, or fragment. Each seed is an HTTPS
origin. The client verifies DNS, TLS, the peer identity, signatures, time limits, topics, and
protocol compatibility before it exchanges claims.

## Discovery

The current runtime has two discovery inputs:

1. `SWARM_BOOTSTRAP_URLS` is a comma-separated list of manual HTTPS seed origins.
2. `SWARM_ALPHA_PACK_PATH` points to a local canonical `SignedAlphaPack` file. The runtime verifies
   its content ID, owner identity, Ed25519 signature, active time window, topics, and version rules.
   It can then use HTTPS endpoints for peers with the `bootstrap` role.

An Alpha Pack is signed configuration. It can name peers, topics, compatibility rules, evidence
minimums, and a suggested local trust policy. Pack membership does not grant trust. A pack cannot
carry code, private keys, raw licensed data, or trade instructions.

Discovery does not yet use Matrix, Farcaster, other social adapters, a DHT, multicast, gossip,
relays, NAT traversal, or blockchain consensus. Nodes also do not learn and forward new peers from
received claims. Operators must install a signed pack or provide seed origins directly. If every
configured seed is unavailable, the node remains attached but has no peers to send to.

Private and loopback addresses are rejected by default. `SWARM_ALLOW_PRIVATE_BOOTSTRAP=true` is an
explicit option for a LAN or same-machine swarm. HTTPS and a certificate trusted by the machine are
still required.

## Publishing scanner observations

`SWARM_PUBLISH_SCANS=false` is the default. Set it to `true` only when this node is allowed to share
its scanner output. After a local scan, the worker signs at most `SWARM_MAX_CLAIMS_PER_SCAN` rows and
sends them only to peers that completed negotiation for the same topic.

A published claim includes the ticker, observation time, scanner and schema versions, scaled setup
and rug scores, trade state, short signals, risk vetoes, and evidence references. The evidence ID is
a hash of a small local receipt containing the snapshot ID, ticker, capture time, and scoring
version. The locator names that local snapshot. Raw price rows, provider responses, API keys, and
licensed source documents are not copied into the swarm claim.

Publishing is best effort. A failed peer delivery is recorded in worker state and does not fail the
local scan. Claims expire after `SWARM_CLAIM_TTL_SECONDS`.

## Receiving claims safely

The transport checks message size, canonical encoding, signatures, node identity, expiry, schema,
topic, and negotiated capabilities. Accepted claims go into `SWARM_PEER_STORE_PATH`, a bounded
peer-only SQLite database. They do not enter the first-party provider tables.

The local trust database at `SWARM_LOCAL_TRUST_STORE_PATH` records outcomes measured by this node
and local key-rotation decisions. Reputation comes from those local outcomes, not from the peer or
Alpha Pack. Repeated evidence from one source family is discounted.

Even an eligible remote claim is `context_only`. The gate always sets trade execution to false. A
remote `TRIGGERED` or `EXIT` state cannot place an order, change the local trade state, or bypass a
local hard veto. The node must verify named evidence locally and pass its own risk rules first.

## Node identity

Each attached node signs manifests and claims with one Ed25519 key. Outside Fly, the runtime loads
or creates an owner-only key file at `SWARM_KEY_PATH`. Keep that file stable and private. Replacing
it creates a new node identity.

On Fly, attached mode requires `SWARM_NODE_PRIVATE_KEY`. Store it as a Fly secret, not in source or
normal environment files. The value is the URL-safe base64 form, without padding, of the raw
32-byte Ed25519 private key. Every Fly machine serving the same public node must receive the same
secret. Otherwise requests to one public URL can return different signed identities.

## Deployment limit

The current runtime is safe for one public attached replica. Its peer claim store and local trust
store are SQLite files, while negotiated peer results live in process memory. Separate Fly machines
do not automatically share those values, even when they share `SWARM_NODE_PRIVATE_KEY` and
`SWARM_PUBLIC_URL`.

Do not scale one public swarm identity across several independently routed web replicas yet. Use
one public attached replica, or first move peer claims, trust decisions, replay protection, bans,
and negotiated peer state to a shared design with clear transaction rules. A persistent volume on
one machine can preserve the SQLite files across restarts, but it does not make them safe shared
state for several machines.

## Environment settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `SWARM_MODE` | `solo` | Use `solo` or `attached`. |
| `SWARM_PUBLIC_URL` | none | Public HTTPS origin advertised by an attached node. Required in attached mode. |
| `SWARM_NODE_PRIVATE_KEY` | none | Shared base64url Ed25519 private key. Required for attached mode when `FLY_APP_NAME` is present. |
| `SWARM_KEY_PATH` | `data/swarm/node.key` | Local node key file used when no key secret is supplied. |
| `SWARM_ALPHA_PACK_PATH` | none | Local canonical signed Alpha Pack used for peer and policy configuration. |
| `SWARM_BOOTSTRAP_URLS` | none | Comma-separated manual HTTPS seed origins, up to 16. |
| `SWARM_TOPICS` | `markets/equities/us/runners` | Comma-separated topics this node supports, up to 64. |
| `SWARM_CLAIM_SCHEMA_VERSIONS` | `runner-v1` | Comma-separated scanner payload schema versions, up to 32. |
| `SWARM_PUBLISH_SCANS` | `false` | Explicitly allow the worker to publish local scan claims. |
| `SWARM_MAX_CLAIMS_PER_SCAN` | `10` | Maximum local scan rows signed per scan, from 1 to 100. |
| `SWARM_CLAIM_TTL_SECONDS` | `900` | Lifetime of a published scan claim, from 60 to 86,400 seconds. |
| `SWARM_MANIFEST_TTL_SECONDS` | `86400` | Lifetime of the signed node manifest, from 60 to 604,800 seconds. |
| `SWARM_BOOTSTRAP_INTERVAL_SECONDS` | `300` | Time between seed refreshes, from 30 to 86,400 seconds. |
| `SWARM_PEER_RATE_LIMIT_PER_MINUTE` | `60` | Per-peer receive limit, from 1 to 10,000 claims per minute. |
| `SWARM_ALLOW_PRIVATE_BOOTSTRAP` | `false` | Allow private or loopback seed addresses while still requiring trusted HTTPS. |
| `SWARM_PEER_STORE_PATH` | `data/swarm/peer-claims.db` | Isolated SQLite store for untrusted peer claims, replay history, bans, and revocations. |
| `SWARM_LOCAL_TRUST_STORE_PATH` | `data/swarm/local-trust.db` | Isolated SQLite store for local outcomes and accepted key rotations. |
| `SWARM_SOFTWARE_VERSION` | `0.1.0` | Software version placed in the signed node manifest and local scan claims. |

Invalid or unsafe values stop runtime startup. Attached mode does not silently fall back to solo.

## Related protocol documents

- [Protocol overview](swarm-protocol.md)
- [HTTPS transport](swarm-transport.md)
- [Node manifests](swarm-node-manifest.md)
- [Signed claims](swarm-signed-claim.md)
- [Alpha Packs](swarm-alpha-pack.md)
- [Peer claim storage](swarm-peer-store.md)
- [Local trust and key rotation](swarm-local-trust.md)
