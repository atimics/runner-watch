from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from starlette.requests import Request

import runner_swarm.transport as swarm_transport
from runner_swarm.node_manifest import (
    NodeEndpoint,
    NodeManifest,
    VersionedDeclaration,
    sign_node_manifest,
)
from runner_swarm.protocol import canonical_json_bytes, node_id_from_public_key, public_key_text
from runner_swarm.signed_claim import (
    EvidenceReferenceV1,
    RunnerObservationV1,
    SignedClaimV1,
    SourceVersionV1,
)
from runner_swarm.transport import (
    CLAIM_EXCHANGE_PATH,
    MANIFEST_MEDIA_TYPE,
    NEGOTIATION_PATH,
    WELL_KNOWN_PATH,
    ClaimExchangeReceipt,
    ClaimExchangeRequest,
    DiscoveryError,
    PeerClaimInbox,
    PeerNegotiationRequest,
    SwarmTransport,
    create_swarm_router,
    fetch_signed_manifest,
    negotiate_with_peer,
    post_claim_to_peer,
    well_known_manifest_url,
)

NOW = datetime(2026, 8, 30, 18, tzinfo=UTC)
TOPIC = "markets/equities/us/runners"
OTHER_TOPIC = "markets/equities/us/risk"
EVIDENCE_ID = "sha256:" + "a" * 64


def _key(seed: int) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def _manifest(
    key: Ed25519PrivateKey,
    *,
    capability: str,
    topics: tuple[str, ...] = (TOPIC,),
    schema_version: str = "1.0.0",
):
    public_key = public_key_text(key)
    manifest = NodeManifest(
        node_id=node_id_from_public_key(public_key),
        public_key=public_key,
        issued_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        software_name="runner-watch",
        software_version="0.1.0",
        capabilities=(VersionedDeclaration(name=capability, version="1.0.0"),),
        endpoints=(NodeEndpoint(transport="https", address="https://scanner.example/swarm"),),
        schema_versions=(VersionedDeclaration(name="rati.signed_claim", version=schema_version),),
        supported_topics=topics,
    )
    return sign_node_manifest(manifest, key)


def _claim(key: Ed25519PrivateKey, *, schema_version: str = "runner-v1") -> SignedClaimV1:
    public_key = public_key_text(key)
    claim = RunnerObservationV1(
        issuer_node_id=node_id_from_public_key(public_key),
        issuer_public_key=public_key,
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=15),
        instrument="NASDAQ:PEN",
        observed_at=NOW,
        scanner_version="market_risk_v3",
        schema_version=schema_version,
        source_versions=(
            SourceVersionV1(family="market", source="massive.bars", version="2026-08"),
        ),
        setup_score_milli=78_500,
        rug_score_milli=23_000,
        rug_level="LOW",
        trade_state="ARMED",
        state_reason="Peer saw a setup; the local risk gate still decides.",
        evidence=(
            EvidenceReferenceV1(
                evidence_id=EVIDENCE_ID,
                family="market",
                source="massive.bars",
                observed_at=NOW,
            ),
        ),
    )
    return SignedClaimV1.sign(claim, key)


@pytest.fixture
def swarm_pair():
    local_key = _key(11)
    peer_key = _key(12)
    local = _manifest(
        local_key,
        capability="claims.receive",
        topics=(TOPIC, OTHER_TOPIC),
        schema_version="1.2.0",
    )
    peer = _manifest(
        peer_key,
        capability="claims.publish",
        topics=(TOPIC,),
        schema_version="1.9.0",
    )
    return local, peer, peer_key


def _router(local_manifest, **router_kwargs):
    return create_swarm_router(local_manifest, clock=lambda: NOW, **router_kwargs)


def _endpoint(router, path: str, method: str):
    return next(
        route.endpoint for route in router.routes if route.path == path and method in route.methods
    )


def _request(body: bytes, content_type: str, *, content_length: int | None = None) -> Request:
    headers = [(b"content-type", content_type.encode())]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    return Request(scope, receive)


def test_router_publishes_exact_signed_manifest(swarm_pair) -> None:
    local, _peer, _peer_key = swarm_pair
    router = _router(local)
    response = _run(_endpoint(router, WELL_KNOWN_PATH, "GET")())

    assert response.status_code == 200
    assert response.body == local.to_wire_bytes()
    assert response.headers["content-type"].startswith(MANIFEST_MEDIA_TYPE)
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_topic_and_schema_negotiation_uses_verified_manifest_intersection(swarm_pair) -> None:
    local, peer, _peer_key = swarm_pair
    request = PeerNegotiationRequest(
        peer_manifest=peer,
        requested_topics=(OTHER_TOPIC, TOPIC),
    )

    router = _router(local)
    response = _run(
        _endpoint(router, NEGOTIATION_PATH, "POST")(
            _request(canonical_json_bytes(request), MANIFEST_MEDIA_TYPE)
        )
    )

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["accepted_topics"] == [TOPIC]
    assert body["rejected_topics"] == [OTHER_TOPIC]
    assert body["compatible_claim_schemas"] == [{"name": "rati.signed_claim", "version": "1.2.0"}]
    assert body["require_local_risk_gate"] is True


def test_claim_exchange_verifies_identity_and_only_calls_untrusted_sink(swarm_pair) -> None:
    local, peer, peer_key = swarm_pair
    received = []
    exchange = ClaimExchangeRequest(
        topic=TOPIC,
        peer_manifest=peer,
        signed_claim=_claim(peer_key),
    )

    router = _router(local, receive_claim=lambda item: received.append(item))
    response = _run(
        _endpoint(router, CLAIM_EXCHANGE_PATH, "POST")(
            _request(canonical_json_bytes(exchange), "application/json")
        )
    )

    assert response.status_code == 202
    body = json.loads(response.body)
    assert body["stored_as_untrusted_peer_claim"] is True
    assert body["require_local_risk_gate"] is True
    assert received[0].topic == TOPIC
    assert received[0].peer_manifest.node_id == peer.manifest.node_id
    assert received[0].signed_claim.claim_id == exchange.signed_claim.claim_id


def test_default_inbox_filters_replayed_claims(swarm_pair) -> None:
    local, peer, peer_key = swarm_pair
    inbox = PeerClaimInbox(max_claims=4)
    transport = SwarmTransport(local, inbox=inbox, clock=lambda: NOW)
    exchange = ClaimExchangeRequest(
        topic=TOPIC,
        peer_manifest=peer,
        signed_claim=_claim(peer_key),
    )

    first = _run(transport.accept_claim(exchange))
    second = _run(transport.accept_claim(exchange))

    assert first.duplicate is False
    assert second.duplicate is True
    assert len(inbox.snapshot()) == 1


def _run(coroutine):
    import asyncio

    return asyncio.run(coroutine)


def test_claim_exchange_rejects_identity_substitution(swarm_pair) -> None:
    local, peer, _peer_key = swarm_pair
    other_key = _key(13)
    exchange = ClaimExchangeRequest(
        topic=TOPIC,
        peer_manifest=peer,
        signed_claim=_claim(other_key),
    )

    router = _router(local)
    with pytest.raises(HTTPException) as exc_info:
        _run(
            _endpoint(router, CLAIM_EXCHANGE_PATH, "POST")(
                _request(canonical_json_bytes(exchange), "application/json")
            )
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid peer claim"


def test_claim_exchange_enforces_local_payload_schema_allowlist(swarm_pair) -> None:
    local, peer, peer_key = swarm_pair
    exchange = ClaimExchangeRequest(
        topic=TOPIC,
        peer_manifest=peer,
        signed_claim=_claim(peer_key, schema_version="runner-v2"),
    )

    router = _router(
        local,
        accepted_claim_schema_versions=frozenset({"runner-v1"}),
    )
    with pytest.raises(HTTPException) as exc_info:
        _run(
            _endpoint(router, CLAIM_EXCHANGE_PATH, "POST")(
                _request(canonical_json_bytes(exchange), "application/json")
            )
        )

    assert exc_info.value.status_code == 400


def test_exchange_rejects_wrong_media_type_and_oversized_declared_body(swarm_pair) -> None:
    local, _peer, _peer_key = swarm_pair
    endpoint = _endpoint(_router(local), CLAIM_EXCHANGE_PATH, "POST")

    with pytest.raises(HTTPException) as wrong_type:
        _run(endpoint(_request(b"{}", "text/plain")))
    with pytest.raises(HTTPException) as too_large:
        _run(
            endpoint(
                _request(
                    b"{}",
                    "application/json",
                    content_length=swarm_transport.MAX_CLAIM_EXCHANGE_BYTES + 1,
                )
            )
        )

    assert wrong_type.value.status_code == 415
    assert too_large.value.status_code == 413


@pytest.mark.parametrize(
    "origin",
    [
        "http://peer.example",
        "https://user:secret@peer.example",
        "https://peer.example:8443",
        "https://peer.example/path",
        "https://peer.example?token=secret",
        "https://localhost",
        "https://scanner.local",
        "https://127.0.0.1",
        "https://single-label",
        "https://peer.example\\@127.0.0.1",
    ],
)
def test_well_known_manifest_url_rejects_unsafe_origins(origin: str) -> None:
    with pytest.raises(DiscoveryError):
        well_known_manifest_url(origin)


def test_well_known_manifest_url_is_fixed() -> None:
    assert well_known_manifest_url("https://PEER.example./") == (
        "https://peer.example/.well-known/rati-swarm.json"
    )


def test_private_discovery_requires_explicit_opt_in(monkeypatch, swarm_pair) -> None:
    _local, peer, _peer_key = swarm_pair
    requested = {}

    def requester(url, addresses, *, timeout_seconds):
        requested["url"] = url
        requested["address"] = addresses[0].sockaddr[0]
        return peer.to_wire_bytes()

    discovered = fetch_signed_manifest(
        "https://localhost",
        at=NOW,
        allow_private_addresses=True,
        resolver=lambda *args, **kwargs: _dns_answers("127.0.0.1"),
        requester=requester,
    )

    assert discovered == peer
    assert requested == {
        "url": "https://localhost/.well-known/rati-swarm.json",
        "address": "127.0.0.1",
    }


def _dns_answers(*addresses: str):
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))
        for address in addresses
    ]


def test_discovery_fetch_pins_public_dns_and_verifies_wire_manifest(
    monkeypatch, swarm_pair
) -> None:
    _local, peer, _peer_key = swarm_pair
    requested = {}

    def resolver(host, port, **kwargs):
        requested["dns"] = (host, port, kwargs)
        return _dns_answers("93.184.216.34")

    def request(url, addresses, *, timeout_seconds):
        requested["request"] = (url, addresses, timeout_seconds)
        return peer.to_wire_bytes()

    monkeypatch.setattr(swarm_transport, "_request_manifest_bytes", request)

    discovered = fetch_signed_manifest(
        "https://peer.example",
        at=NOW,
        timeout_seconds=2.0,
        resolver=resolver,
    )

    assert discovered == peer
    assert requested["dns"][0:2] == ("peer.example", 443)
    assert requested["request"][0] == "https://peer.example/.well-known/rati-swarm.json"
    assert requested["request"][2] == 2.0


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("10.1.2.3",),
        ("169.254.169.254",),
        ("93.184.216.34", "192.168.1.20"),
    ],
)
def test_discovery_rejects_private_or_mixed_dns_answers(monkeypatch, addresses) -> None:
    def should_not_request(*args, **kwargs):
        raise AssertionError("unsafe address must be rejected before a connection")

    monkeypatch.setattr(swarm_transport, "_request_manifest_bytes", should_not_request)

    with pytest.raises(DiscoveryError, match="non-public"):
        fetch_signed_manifest(
            "https://peer.example",
            resolver=lambda *args, **kwargs: _dns_answers(*addresses),
        )


def test_discovery_rejects_noncanonical_signed_manifest(monkeypatch, swarm_pair) -> None:
    _local, peer, _peer_key = swarm_pair
    noncanonical = json.dumps(peer.model_dump(mode="json")).encode()
    monkeypatch.setattr(
        swarm_transport,
        "_request_manifest_bytes",
        lambda *args, **kwargs: noncanonical,
    )

    with pytest.raises(DiscoveryError, match="verification failed"):
        fetch_signed_manifest(
            "https://peer.example",
            at=NOW,
            resolver=lambda *args, **kwargs: _dns_answers("93.184.216.34"),
        )


def test_outbound_negotiation_uses_pinned_https_and_checks_node_ids(swarm_pair) -> None:
    receiver_manifest, sender_manifest, _sender_key = swarm_pair
    receiver = SwarmTransport(receiver_manifest, clock=lambda: NOW)
    requested = {}

    def requester(url, addresses, **kwargs):
        requested["url"] = url
        requested["address"] = addresses[0].sockaddr[0]
        requested["status"] = kwargs["expected_status"]
        incoming = PeerNegotiationRequest.model_validate_json(kwargs["body"])
        return canonical_json_bytes(receiver.negotiate(incoming))

    response = negotiate_with_peer(
        "https://receiver.internal",
        sender_manifest,
        (TOPIC,),
        expected_peer_node_id=receiver_manifest.manifest.node_id,
        at=NOW,
        allow_private_addresses=True,
        resolver=lambda *args, **kwargs: _dns_answers("192.168.1.25"),
        requester=requester,
    )

    assert response.accepted_topics == (TOPIC,)
    assert requested == {
        "url": "https://receiver.internal/swarm/v1/negotiate",
        "address": "192.168.1.25",
        "status": 200,
    }


def test_outbound_claim_post_delivers_verified_claim_and_checks_receipt(swarm_pair) -> None:
    receiver_manifest, sender_manifest, sender_key = swarm_pair
    inbox = PeerClaimInbox(max_claims=4)
    receiver = SwarmTransport(receiver_manifest, inbox=inbox, clock=lambda: NOW)
    signed_claim = _claim(sender_key)

    def requester(url, addresses, **kwargs):
        assert url == "https://receiver.example/swarm/v1/claims"
        assert addresses[0].sockaddr[0] == "93.184.216.34"
        assert kwargs["expected_status"] == 202
        incoming = ClaimExchangeRequest.model_validate_json(kwargs["body"])
        receipt = _run(receiver.accept_claim(incoming))
        return canonical_json_bytes(receipt)

    receipt = post_claim_to_peer(
        "https://receiver.example",
        sender_manifest,
        signed_claim,
        TOPIC,
        expected_peer_node_id=receiver_manifest.manifest.node_id,
        at=NOW,
        resolver=lambda *args, **kwargs: _dns_answers("93.184.216.34"),
        requester=requester,
    )

    assert receipt.claim_id == signed_claim.claim_id
    assert receipt.require_local_risk_gate is True
    assert inbox.snapshot()[0].signed_claim == signed_claim


def test_outbound_claim_post_rejects_mismatched_receipt(swarm_pair) -> None:
    receiver_manifest, sender_manifest, sender_key = swarm_pair
    signed_claim = _claim(sender_key)
    wrong_receipt = ClaimExchangeReceipt(
        duplicate=False,
        claim_id="sha256:" + "b" * 64,
        local_node_id=receiver_manifest.manifest.node_id,
        peer_node_id=sender_manifest.manifest.node_id,
        topic=TOPIC,
    )

    with pytest.raises(swarm_transport.PeerExchangeError, match="does not match"):
        post_claim_to_peer(
            "https://receiver.example",
            sender_manifest,
            signed_claim,
            TOPIC,
            at=NOW,
            resolver=lambda *args, **kwargs: _dns_answers("93.184.216.34"),
            requester=lambda *args, **kwargs: canonical_json_bytes(wrong_receipt),
        )
