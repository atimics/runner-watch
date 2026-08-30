from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from runner_swarm.node_manifest import (
    MANIFEST_TYPE,
    PROTOCOL_VERSION,
    ManifestVerificationError,
    NodeEndpoint,
    NodeManifest,
    SignedNodeManifest,
    VersionedDeclaration,
    node_id_from_public_key,
    public_key_base64,
    sign_node_manifest,
    verify_signed_node_manifest,
)

NOW = datetime(2026, 8, 29, 18, 0, tzinfo=UTC)


def _private_key(seed: int = 7) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)


def _manifest(
    *, private_key: Ed25519PrivateKey | None = None, expires_at: datetime | None = None
) -> NodeManifest:
    key = private_key or _private_key()
    public_key = public_key_base64(key.public_key())
    return NodeManifest(
        message_type=MANIFEST_TYPE,
        protocol_version=PROTOCOL_VERSION,
        node_id=node_id_from_public_key(public_key),
        public_key=public_key,
        issued_at=NOW,
        expires_at=expires_at or NOW + timedelta(hours=1),
        software_name="runner-watch",
        software_version="0.1.0",
        capabilities=(
            VersionedDeclaration(name="claims.publish", version="1.0.0"),
            VersionedDeclaration(name="discovery.bootstrap", version="1.0.0"),
        ),
        endpoints=(
            NodeEndpoint(
                transport="libp2p",
                address="/dns4/seed.example.com/tcp/443/wss",
            ),
            NodeEndpoint(transport="https", address="https://seed.example.com/swarm"),
        ),
        schema_versions=(
            VersionedDeclaration(name="rati.signed_claim", version="1.0.0"),
            VersionedDeclaration(name="rati.alpha_pack", version="1.0.0"),
        ),
        supported_topics=("markets/equities/us/runners", "markets/equities/us/risk"),
    )


def test_signed_manifest_round_trip_and_verification() -> None:
    signed = sign_node_manifest(_manifest(), _private_key())

    wire_bytes = signed.canonical_bytes()
    restored = SignedNodeManifest.model_validate_json(wire_bytes)

    assert verify_signed_node_manifest(restored, at=NOW) == signed.manifest
    assert restored.content_id.startswith("sha256:")
    assert "=" not in restored.manifest.public_key
    assert "=" not in restored.signature


@pytest.mark.parametrize("tampered_field", ["payload", "content_id", "signature"])
def test_verification_rejects_tampering(tampered_field: str) -> None:
    signed = sign_node_manifest(_manifest(), _private_key())
    wire = signed.model_dump(mode="json")

    if tampered_field == "payload":
        wire["manifest"]["supported_topics"] = ["markets/equities/us/halts"]
    elif tampered_field == "content_id":
        wire["content_id"] = "sha256:" + "0" * 64
    else:
        raw_signature = bytearray(
            base64.urlsafe_b64decode(wire["signature"] + "=" * (-len(wire["signature"]) % 4))
        )
        raw_signature[0] ^= 1
        wire["signature"] = base64.urlsafe_b64encode(raw_signature).rstrip(b"=").decode()

    tampered = SignedNodeManifest.model_validate_json(json.dumps(wire))
    with pytest.raises(ManifestVerificationError):
        verify_signed_node_manifest(tampered, at=NOW)


def test_verification_rejects_expired_manifest() -> None:
    signed = sign_node_manifest(_manifest(expires_at=NOW + timedelta(minutes=1)), _private_key())

    with pytest.raises(ManifestVerificationError, match="expired"):
        verify_signed_node_manifest(signed, at=NOW + timedelta(minutes=1))


@pytest.mark.parametrize(
    ("transport", "address"),
    [
        ("https", "http://seed.example.com/swarm"),
        ("wss", "wss://user:secret@seed.example.com/swarm"),
        ("https", "https://seed.example.com/swarm?token=secret"),
        ("https", "https://seed example.com/swarm"),
        ("libp2p", "seed.example.com/tcp/443"),
        ("libp2p", "/p2p"),
        ("libp2p", "/udp/4001/quic-v1"),
    ],
)
def test_invalid_endpoints_are_rejected(transport: str, address: str) -> None:
    with pytest.raises(ValidationError):
        NodeEndpoint.model_validate({"transport": transport, "address": address})


@pytest.mark.parametrize(
    "topic",
    ["Markets/Equities", "markets//equities", " markets/equities", "../private"],
)
def test_invalid_topics_are_rejected(topic: str) -> None:
    data = _manifest().model_dump()
    data["supported_topics"] = (topic,)

    with pytest.raises(ValidationError):
        NodeManifest.model_validate(data)


@pytest.mark.parametrize("version", ["1", "v1.2.3", "1.2", "01.2.3", "1.2.3-01", "1.2.3?"])
def test_invalid_software_and_schema_versions_are_rejected(version: str) -> None:
    data = _manifest().model_dump()
    data["software_version"] = version
    with pytest.raises(ValidationError):
        NodeManifest.model_validate(data)

    with pytest.raises(ValidationError):
        VersionedDeclaration(name="rati.signed_claim", version=version)


def test_invalid_protocol_version_is_rejected() -> None:
    data = _manifest().model_dump()
    data["protocol_version"] = "2"

    with pytest.raises(ValidationError):
        NodeManifest.model_validate(data)


def test_canonical_bytes_are_deterministic_across_declaration_order() -> None:
    original = _manifest()
    data = original.model_dump()
    data["capabilities"] = tuple(reversed(data["capabilities"]))
    data["endpoints"] = tuple(reversed(data["endpoints"]))
    data["schema_versions"] = tuple(reversed(data["schema_versions"]))
    data["supported_topics"] = tuple(reversed(data["supported_topics"]))
    reordered = NodeManifest.model_validate(data)

    assert reordered.canonical_bytes() == original.canonical_bytes()
    assert reordered.content_id == original.content_id
    assert b" " not in original.canonical_bytes()
    assert b"\n" not in original.canonical_bytes()


def test_identity_must_match_public_key_and_signing_key() -> None:
    data = _manifest().model_dump()
    data["node_id"] = node_id_from_public_key(_private_key(8).public_key())
    with pytest.raises(ValidationError, match="node_id does not match"):
        NodeManifest.model_validate(data)

    with pytest.raises(ValueError, match="private key does not belong"):
        sign_node_manifest(_manifest(), _private_key(8))


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    manifest = _manifest()
    with pytest.raises(ValidationError):
        manifest.software_version = "0.2.0"

    data = manifest.model_dump()
    data["private_address"] = "192.168.1.10"
    with pytest.raises(ValidationError):
        NodeManifest.model_validate(data)
