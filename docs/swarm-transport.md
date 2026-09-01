# RATi swarm HTTPS transport v1

The transport layer makes the signed swarm contracts reachable without giving peers any trading
authority. It supports public manifest discovery, topic negotiation, and bounded claim delivery.
Matrix, Farcaster, social discovery, relays, and order exchange are not part of this layer.

The implementation is in `runner_swarm.transport`. Runtime code supplies its signed manifest and a
callback for accepted peer claims. This keeps key storage, deployment settings, and durable peer
storage outside the transport.

## Server routes

Mount `create_swarm_router(...)` on the FastAPI application:

```python
from runner_swarm.transport import ReceivedPeerClaim, create_swarm_router


def receive_untrusted_claim(received: ReceivedPeerClaim) -> bool:
    # Put this in a peer-only inbox. Do not write it into provider evidence tables.
    peer_claim_store.put(received)
    return True


app.include_router(
    create_swarm_router(
        signed_manifest,
        receive_claim=receive_untrusted_claim,
        accepted_claim_schema_versions=frozenset({"runner-v1"}),
    )
)
```

The local manifest must advertise `claims.receive` at semantic major version 1. A sending peer
must advertise `claims.publish` at semantic major version 1. Both manifests must advertise a
compatible major version of the `rati.signed_claim` schema.

The router adds three routes:

- `GET /.well-known/rati-swarm.json` returns the exact canonical signed-manifest bytes. Its cache
  age is at most five minutes and never extends beyond manifest expiry.
- `POST /swarm/v1/negotiate` accepts a signed peer manifest and requested topics. It returns only
  topics present in both manifests, compatible claim declarations, and the message-size limit.
- `POST /swarm/v1/claims` accepts a topic, signed peer manifest, and signed claim. It returns a
  receipt only after all signatures, expiry times, identity links, topic support, capabilities,
  schema compatibility, and the optional local payload-schema allowlist pass.

Both POST routes require `application/json` or `application/rati-swarm+json`. Request streams are
stopped as soon as their byte limit is exceeded; a misleading `Content-Length` does not bypass the
stream limit. Models reject unknown fields.

The default `PeerClaimInbox` is a small in-memory replay filter and volatile queue. A duplicate
claim gets a successful receipt with `duplicate: true`, so peers do not need to retry it. Production
runtime code can supply a sync or async callback for a durable, bounded peer-only store. Returning
`False` tells the receipt that the claim was already present.

## Safe discovery client

Use a public HTTPS origin, not an arbitrary URL:

```python
from runner_swarm.transport import fetch_signed_manifest

peer = fetch_signed_manifest("https://scanner.example")
```

After discovery, an attached node can negotiate and send through the same pinned HTTPS policy:

```python
from runner_swarm.transport import negotiate_with_peer, post_claim_to_peer

agreement = negotiate_with_peer(
    "https://scanner.example",
    local_signed_manifest,
    ("markets/equities/us/runners",),
    expected_peer_node_id=peer.manifest.node_id,
)
if "markets/equities/us/runners" in agreement.accepted_topics:
    receipt = post_claim_to_peer(
        "https://scanner.example",
        local_signed_manifest,
        local_signed_claim,
        "markets/equities/us/runners",
        expected_peer_node_id=peer.manifest.node_id,
    )
```

Outbound helpers verify the local manifest and claim before sending. They then require canonical,
bounded responses that name the sending node, expected receiving node, claim ID, and topic. The
receipts are protected by HTTPS but are not signed reputation records.

The client always requests `/.well-known/rati-swarm.json`. It rejects credentials, query strings,
fragments, custom paths, non-443 ports, local hostname suffixes, control characters, and non-public
IP literals. DNS is resolved before connecting. If any answer is loopback, private, link-local,
reserved, multicast, or otherwise non-public, the whole request fails. The connection is pinned to
the checked address while TLS certificate validation and SNI still use the original hostname. This
closes the normal DNS-rebinding gap between validation and connection.

The client does not use environment proxies, does not follow redirects, does not accept compressed
responses, and requires a supported JSON content type. Network work is capped at ten seconds and
the response at the signed-manifest wire limit. The OS resolver may have its own DNS timeout. The
returned manifest is accepted only in canonical wire form and after its content ID, identity,
signature, issue time, and expiry time verify.

Private and loopback addresses remain blocked by default. A user who deliberately runs a LAN or
same-machine swarm can pass `allow_private_addresses=True` to discovery, negotiation, and claim
posting. Runtime configuration may expose that as `SWARM_ALLOW_PRIVATE_BOOTSTRAP`. This opt-in does
not allow HTTP, credentials, redirects, query strings, custom ports, oversized bodies, unspecified
addresses, or multicast targets. TLS and hostname verification remain required; private swarms
must use certificates trusted by the local machine.

## Trust boundary

Every response and receipt says that the local risk gate remains required. That is also a runtime
rule, not just metadata:

- A valid signature proves key control and message integrity, not truth or profitability.
- A negotiated topic grants routing compatibility, not trust, reputation, or pack membership.
- Received claims stay separate from market bars, SEC facts, provider provenance, and other trusted
  evidence.
- A remote `TRIGGERED` state is still an untrusted observation. It cannot place an order or bypass a
  local veto.
- Rate limits, peer bans, durable replay history, peer outcome scoring, and key rotation are local
  runtime policy and should wrap the callback. The transport does not create global consensus.
