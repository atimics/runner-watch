from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal, Self
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import (
    Field,
    field_validator,
    model_validator,
)

from runner_swarm.protocol import (
    CONTENT_ID_PATTERN,
    NODE_ID_PATTERN,
    PROTOCOL_VERSION,
    SIGNATURE_ALGORITHM,
    SwarmModel,
    canonical_json_bytes,
    content_id,
    decode_base64url,
    encode_base64url,
    node_id_from_public_key,
    normalize_utc,
    public_key_text,
    signature_domain,
)

MANIFEST_TYPE = "rati.node_manifest"
MAX_MANIFEST_BYTES = 16_384
MAX_SIGNED_MANIFEST_BYTES = 20_480
MAX_MANIFEST_LIFETIME = timedelta(days=7)
DEFAULT_CLOCK_SKEW = timedelta(minutes=5)

_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*$")
_VERSION_PATTERN = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class ManifestVerificationError(ValueError):
    pass


def _to_utc(value: datetime, *, field_name: str) -> datetime:
    normalized = normalize_utc(value, field_name=field_name)
    if normalized.microsecond:
        raise ValueError(f"{field_name} must use whole seconds")
    return normalized


def _validate_name(value: str, *, field_name: str) -> str:
    if not _NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} must be a lowercase name with single '.', '/', '_' or '-' separators"
        )
    return value


def _validate_version(value: str, *, field_name: str) -> str:
    if not _VERSION_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a semantic version such as 1.2.3")
    prerelease = value.split("+", 1)[0].partition("-")[2]
    if any(
        part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease.split(".")
    ):
        raise ValueError(f"{field_name} cannot contain a zero-padded prerelease number")
    return value


def public_key_base64(public_key: Ed25519PublicKey) -> str:

    return public_key_text(public_key)


class VersionedDeclaration(SwarmModel):
    name: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=5, max_length=64)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_name(value, field_name="declaration name")

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        return _validate_version(value, field_name="declaration version")


class NodeEndpoint(SwarmModel):
    transport: Literal["https", "wss", "libp2p"]
    address: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_address(self) -> Self:
        if self.transport == "libp2p":
            if not self.address.startswith("/") or any(char.isspace() for char in self.address):
                raise ValueError("A libp2p endpoint must be a whitespace-free multiaddress")
            parts = self.address.split("/")[1:]
            if len(parts) < 2 or any(not part for part in parts):
                raise ValueError("A libp2p endpoint must include a protocol and address")
            first_protocol = parts[0]
            if first_protocol not in {"dns", "dns4", "dns6", "ip4", "ip6", "p2p"}:
                raise ValueError("A libp2p endpoint must start with a supported address protocol")
            return self

        if any(char.isspace() for char in self.address):
            raise ValueError("Endpoint URLs cannot contain whitespace")
        try:
            parsed = urlsplit(self.address)
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("Endpoint URL is malformed") from exc
        if parsed.scheme != self.transport or not parsed.hostname:
            raise ValueError(f"A {self.transport} endpoint must use {self.transport}:// and a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Endpoint URLs cannot contain credentials, queries, or fragments")
        return self


class NodeManifest(SwarmModel):
    message_type: Literal["rati.node_manifest"] = MANIFEST_TYPE
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    node_id: str = Field(min_length=74, max_length=74)
    public_key: str = Field(min_length=43, max_length=43)
    issued_at: datetime
    expires_at: datetime
    software_name: str = Field(min_length=1, max_length=64)
    software_version: str = Field(min_length=5, max_length=64)
    capabilities: tuple[VersionedDeclaration, ...] = Field(min_length=1, max_length=32)
    endpoints: tuple[NodeEndpoint, ...] = Field(min_length=1, max_length=16)
    schema_versions: tuple[VersionedDeclaration, ...] = Field(min_length=1, max_length=32)
    supported_topics: tuple[str, ...] = Field(min_length=1, max_length=64)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime, info: object) -> datetime:
        field_name = getattr(info, "field_name", "timestamp")
        return _to_utc(value, field_name=field_name)

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("node_id must use the rati-node:<sha256> format")
        return value

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        decode_base64url(value, label="public_key", expected_bytes=32)
        return value

    @field_validator("software_name")
    @classmethod
    def validate_software_name(cls, value: str) -> str:
        return _validate_name(value, field_name="software_name")

    @field_validator("software_version")
    @classmethod
    def validate_software_version(cls, value: str) -> str:
        return _validate_version(value, field_name="software_version")

    @field_validator("capabilities", "schema_versions")
    @classmethod
    def sort_declarations(
        cls, value: tuple[VersionedDeclaration, ...], info: object
    ) -> tuple[VersionedDeclaration, ...]:
        keys = [(item.name, item.version) for item in value]
        if len(keys) != len(set(keys)):
            field_name = getattr(info, "field_name", "declarations")
            raise ValueError(f"{field_name} cannot contain duplicates")
        return tuple(sorted(value, key=lambda item: (item.name, item.version)))

    @field_validator("endpoints")
    @classmethod
    def sort_endpoints(cls, value: tuple[NodeEndpoint, ...]) -> tuple[NodeEndpoint, ...]:
        keys = [(item.transport, item.address) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("endpoints cannot contain duplicates")
        return tuple(sorted(value, key=lambda item: (item.transport, item.address)))

    @field_validator("supported_topics")
    @classmethod
    def validate_topics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for topic in value:
            if not 1 <= len(topic) <= 96:
                raise ValueError("Each supported topic must contain 1 to 96 characters")
            _validate_name(topic, field_name="supported topic")
        if len(value) != len(set(value)):
            raise ValueError("supported_topics cannot contain duplicates")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.node_id != node_id_from_public_key(self.public_key):
            raise ValueError("node_id does not match public_key")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if self.expires_at - self.issued_at > MAX_MANIFEST_LIFETIME:
            raise ValueError("A node manifest cannot be valid for more than 7 days")
        if len(canonical_json_bytes(self)) > MAX_MANIFEST_BYTES:
            raise ValueError(f"A node manifest cannot exceed {MAX_MANIFEST_BYTES} bytes")
        return self

    def canonical_bytes(self) -> bytes:

        return canonical_json_bytes(self)

    @property
    def content_id(self) -> str:
        return content_id(self.canonical_bytes())


class SignedNodeManifest(SwarmModel):
    manifest: NodeManifest
    content_id: str = Field(min_length=71, max_length=71)
    signature_algorithm: Literal["ed25519"] = SIGNATURE_ALGORITHM
    signature: str = Field(min_length=86, max_length=86)

    @field_validator("content_id")
    @classmethod
    def validate_content_id(cls, value: str) -> str:
        if not CONTENT_ID_PATTERN.fullmatch(value):
            raise ValueError("content_id must use the sha256:<hex> format")
        return value

    @field_validator("signature")
    @classmethod
    def validate_signature(cls, value: str) -> str:
        decode_base64url(value, label="signature", expected_bytes=64)
        return value

    def canonical_bytes(self) -> bytes:

        return canonical_json_bytes(self)

    def to_wire_bytes(self) -> bytes:
        wire_bytes = self.canonical_bytes()
        if len(wire_bytes) > MAX_SIGNED_MANIFEST_BYTES:
            raise ValueError("Signed node manifest is too large")
        return wire_bytes

    @classmethod
    def from_wire_bytes(cls, wire_bytes: bytes) -> SignedNodeManifest:
        if len(wire_bytes) > MAX_SIGNED_MANIFEST_BYTES:
            raise ValueError("Signed node manifest is too large")
        try:
            value = cls.model_validate_json(wire_bytes)
        except UnicodeDecodeError as error:
            raise ValueError("Signed node manifest must be UTF-8 JSON") from error
        if value.to_wire_bytes() != wire_bytes:
            raise ValueError("Signed node manifest JSON is not in canonical wire form")
        return value


def sign_node_manifest(
    manifest: NodeManifest, private_key: Ed25519PrivateKey
) -> SignedNodeManifest:

    signer_public_key = public_key_base64(private_key.public_key())
    if signer_public_key != manifest.public_key:
        raise ValueError("The private key does not belong to the manifest public_key")
    signature = private_key.sign(signature_domain(MANIFEST_TYPE) + manifest.canonical_bytes())
    return SignedNodeManifest(
        manifest=manifest,
        content_id=manifest.content_id,
        signature=encode_base64url(signature),
    )


def verify_signed_node_manifest(
    signed: SignedNodeManifest,
    *,
    at: datetime | None = None,
    clock_skew: timedelta = DEFAULT_CLOCK_SKEW,
) -> NodeManifest:

    if clock_skew < timedelta(0):
        raise ValueError("clock_skew cannot be negative")

    checked_at = normalize_utc(at or datetime.now(UTC), field_name="at")
    manifest = signed.manifest

    if signed.content_id != manifest.content_id:
        raise ManifestVerificationError("Manifest content ID does not match its payload")
    if manifest.issued_at > checked_at + clock_skew:
        raise ManifestVerificationError("Manifest was issued too far in the future")
    if manifest.expires_at <= checked_at:
        raise ManifestVerificationError("Manifest has expired")

    public_key_bytes = decode_base64url(manifest.public_key, label="public_key", expected_bytes=32)
    signature = decode_base64url(signed.signature, label="signature", expected_bytes=64)
    public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
    try:
        public_key.verify(signature, signature_domain(MANIFEST_TYPE) + manifest.canonical_bytes())
    except InvalidSignature as exc:
        raise ManifestVerificationError("Manifest signature is invalid") from exc
    return manifest
