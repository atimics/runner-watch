from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import Field, field_validator, model_validator

from runner_swarm.protocol import (
    CONTENT_ID_PATTERN,
    PROTOCOL_VERSION,
    SIGNATURE_ALGORITHM,
    NodeIdentity,
    SwarmModel,
    canonical_json_bytes,
    content_id,
    decode_base64url,
    encode_base64url,
    normalize_utc,
    public_key_text,
    signature_domain,
)

MESSAGE_TYPE = "rati.alpha_pack"
MAX_PACK_BYTES = 256 * 1024
MAX_SIGNED_PACK_BYTES = MAX_PACK_BYTES + 4096
MAX_PACK_LIFETIME = timedelta(days=366)
MAX_PEERS = 128
MAX_TOPICS = 256
MAX_ENDPOINTS_PER_PEER = 8
MAX_RECIPIENT_KEYS = 256

_PACK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")

ShortText = Annotated[str, Field(min_length=1, max_length=128)]
VersionToken = Annotated[str, Field(min_length=1, max_length=128)]


class AlphaPackError(ValueError):
    pass


class PackVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


class PeerRole(StrEnum):
    APPROVED = "approved"
    BOOTSTRAP = "bootstrap"


class PackStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class FrozenModel(SwarmModel):
    pass


def _utc(value: datetime) -> datetime:
    return normalize_utc(value)


def _clean(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


def _clean_nonempty(value: str, *, label: str) -> str:
    value = _clean(value)
    if not value:
        raise ValueError(f"{label} cannot be empty")
    return value


class PeerReference(NodeIdentity):
    roles: tuple[PeerRole, ...]
    endpoints: tuple[str, ...] = ()

    @field_validator("roles", mode="before")
    @classmethod
    def normalize_roles(cls, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            value = (value,)
        normalized = tuple(
            sorted(str(item.value if isinstance(item, PeerRole) else item) for item in value)
        )
        if not normalized:
            raise ValueError("A peer needs at least one role")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Peer roles must be unique")
        return normalized

    @field_validator("endpoints", mode="before")
    @classmethod
    def normalize_endpoints(cls, value: Any) -> tuple[str, ...]:
        endpoints = tuple(sorted(_clean(str(item)) for item in value))
        if len(endpoints) > MAX_ENDPOINTS_PER_PEER:
            raise ValueError(f"A peer may have at most {MAX_ENDPOINTS_PER_PEER} endpoints")
        if any(not endpoint or len(endpoint) > 512 for endpoint in endpoints):
            raise ValueError("Peer endpoints must contain 1 to 512 characters")
        if len(set(endpoints)) != len(endpoints):
            raise ValueError("Peer endpoints must be unique")
        return endpoints

    @model_validator(mode="after")
    def require_bootstrap_endpoint(self) -> PeerReference:
        if PeerRole.BOOTSTRAP in self.roles and not self.endpoints:
            raise ValueError("Bootstrap peers require at least one endpoint")
        return self


class LocalTrustPolicy(FrozenModel):
    membership_grants_trust: Literal[False] = False
    initial_peer_weight_ppm: Annotated[int, Field(ge=0, le=250_000)] = 0
    minimum_reputation_ppm: Annotated[int, Field(ge=0, le=1_000_000)] = 600_000
    minimum_scored_outcomes: Annotated[int, Field(ge=0, le=100_000)] = 20
    maximum_peer_claim_weight_ppm: Annotated[int, Field(gt=0, le=1_000_000)] = 250_000
    require_local_risk_gate: Literal[True] = True

    @model_validator(mode="after")
    def validate_weights(self) -> LocalTrustPolicy:
        if self.initial_peer_weight_ppm > self.maximum_peer_claim_weight_ppm:
            raise ValueError("initial_peer_weight_ppm cannot exceed maximum_peer_claim_weight_ppm")
        return self


class EvidenceRequirements(FrozenModel):
    minimum_receipts: Annotated[int, Field(ge=0, le=64)] = 1
    minimum_independent_source_families: Annotated[int, Field(ge=0, le=32)] = 1
    require_content_hashes: bool = True
    accepted_digest_algorithms: tuple[Literal["sha256"], ...] = ("sha256",)
    maximum_evidence_age_seconds: Annotated[int, Field(ge=1, le=31_536_000)] = 86_400
    allow_licensed_raw_data_redistribution: Literal[False] = False

    @field_validator("accepted_digest_algorithms", mode="before")
    @classmethod
    def unique_digest_algorithms(cls, value: Any) -> tuple[str, ...]:
        algorithms = tuple(sorted(str(item) for item in value))
        if not algorithms:
            raise ValueError("At least one evidence digest algorithm is required")
        if len(set(algorithms)) != len(algorithms):
            raise ValueError("Evidence digest algorithms must be unique")
        return algorithms

    @model_validator(mode="after")
    def validate_evidence_counts(self) -> EvidenceRequirements:
        if self.minimum_independent_source_families > self.minimum_receipts:
            raise ValueError("Independent source families cannot exceed evidence receipts")
        if self.require_content_hashes and self.minimum_receipts == 0:
            raise ValueError("Content hashes require at least one evidence receipt")
        return self


class PrivatePackEncryption(FrozenModel):
    scheme: Literal["x25519-xchacha20poly1305"] = "x25519-xchacha20poly1305"
    group_key_id: ShortText
    recipient_key_ids: tuple[ShortText, ...]
    encrypted_payload_locator: Annotated[str, Field(min_length=1, max_length=512)] | None = None

    @field_validator("group_key_id", "encrypted_payload_locator")
    @classmethod
    def normalize_text(cls, value: str | None) -> str | None:
        return _clean_nonempty(value, label="Encryption metadata") if value is not None else None

    @field_validator("recipient_key_ids", mode="before")
    @classmethod
    def normalize_recipient_ids(cls, value: Any) -> tuple[str, ...]:
        identifiers = tuple(sorted(_clean(str(item)) for item in value))
        if not identifiers:
            raise ValueError("Private packs require at least one recipient key ID")
        if any(not identifier for identifier in identifiers):
            raise ValueError("Recipient key IDs cannot be empty")
        if len(identifiers) > MAX_RECIPIENT_KEYS:
            raise ValueError(f"A pack may name at most {MAX_RECIPIENT_KEYS} recipient keys")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Recipient key IDs must be unique")
        return identifiers


class AlphaPack(FrozenModel):
    message_type: Literal["rati.alpha_pack"] = MESSAGE_TYPE
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    pack_id: str
    pack_version: Annotated[int, Field(ge=1, le=2_147_483_647)]
    name: ShortText
    description: Annotated[str, Field(max_length=1024)] = ""
    owner: NodeIdentity
    visibility: PackVisibility
    status: PackStatus = PackStatus.ACTIVE
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    supersedes_content_id: str | None = None
    peers: tuple[PeerReference, ...] = ()
    topics: tuple[str, ...]
    allowed_claim_versions: tuple[VersionToken, ...]
    allowed_schema_versions: tuple[VersionToken, ...]
    trust_policy: LocalTrustPolicy = Field(default_factory=LocalTrustPolicy)
    evidence_requirements: EvidenceRequirements = Field(default_factory=EvidenceRequirements)
    private_encryption: PrivatePackEncryption | None = None

    @field_validator("issued_at", "expires_at", "revoked_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @field_validator("pack_id")
    @classmethod
    def validate_pack_id(cls, value: str) -> str:
        value = _clean(value).lower()
        if not _PACK_ID.fullmatch(value):
            raise ValueError("pack_id must be a lowercase 3-64 character slug")
        return value

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return _clean_nonempty(value, label="name")

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return _clean(value)

    @field_validator("supersedes_content_id")
    @classmethod
    def validate_supersedes_id(cls, value: str | None) -> str | None:
        if value is not None and not CONTENT_ID_PATTERN.fullmatch(value):
            raise ValueError("supersedes_content_id must be a sha256 content ID")
        return value

    @field_validator("peers", mode="before")
    @classmethod
    def bound_peers(cls, value: Any) -> Any:
        if len(value) > MAX_PEERS:
            raise ValueError(f"An alpha pack may contain at most {MAX_PEERS} peers")
        return value

    @field_validator("peers")
    @classmethod
    def sort_peers(cls, value: tuple[PeerReference, ...]) -> tuple[PeerReference, ...]:
        return tuple(sorted(value, key=lambda peer: peer.node_id))

    @field_validator("topics", mode="before")
    @classmethod
    def normalize_topics(cls, value: Any) -> tuple[str, ...]:
        topics = tuple(sorted(_clean(str(item)).lower() for item in value))
        if not topics:
            raise ValueError("An alpha pack requires at least one topic")
        if len(topics) > MAX_TOPICS:
            raise ValueError(f"An alpha pack may contain at most {MAX_TOPICS} topics")
        if len(set(topics)) != len(topics):
            raise ValueError("Topics must be unique")
        if any(not _TOKEN.fullmatch(topic) for topic in topics):
            raise ValueError("Topics must be portable protocol tokens")
        return topics

    @field_validator("allowed_claim_versions", "allowed_schema_versions", mode="before")
    @classmethod
    def normalize_versions(cls, value: Any) -> tuple[str, ...]:
        versions = tuple(sorted(_clean(str(item)) for item in value))
        if not versions or len(versions) > 32:
            raise ValueError("Version allowlists must contain between 1 and 32 entries")
        if len(set(versions)) != len(versions):
            raise ValueError("Version allowlist entries must be unique")
        if any(not _TOKEN.fullmatch(version) for version in versions):
            raise ValueError("Version allowlists must contain portable protocol tokens")
        return versions

    @model_validator(mode="after")
    def validate_pack(self) -> AlphaPack:
        peer_ids = [peer.node_id for peer in self.peers]
        if len(set(peer_ids)) != len(peer_ids):
            raise ValueError("Peer node IDs must be unique")
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if self.expires_at - self.issued_at > MAX_PACK_LIFETIME:
            raise ValueError("An alpha pack cannot be valid for more than 366 days")
        if self.status == PackStatus.REVOKED and self.revoked_at is None:
            raise ValueError("A revoked pack requires revoked_at")
        if self.status == PackStatus.ACTIVE and self.revoked_at is not None:
            raise ValueError("An active pack cannot have revoked_at")
        if self.revoked_at is not None and self.revoked_at < self.issued_at:
            raise ValueError("revoked_at cannot be earlier than issued_at")
        if self.visibility == PackVisibility.PRIVATE and self.private_encryption is None:
            raise ValueError("Private packs require encryption metadata")
        if self.visibility == PackVisibility.PUBLIC and self.private_encryption is not None:
            raise ValueError("Public packs cannot include private encryption metadata")
        if len(canonical_json_bytes(self)) > MAX_PACK_BYTES:
            raise ValueError(f"Canonical alpha pack exceeds {MAX_PACK_BYTES} bytes")
        return self

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    @property
    def content_id(self) -> str:
        return content_id(self.canonical_bytes)

    def is_active(self, at: datetime | None = None) -> bool:
        at = _utc(at or datetime.now(UTC))
        return (
            self.status == PackStatus.ACTIVE
            and self.revoked_at is None
            and self.issued_at <= at < self.expires_at
        )


class SignedAlphaPack(FrozenModel):
    pack: AlphaPack
    content_id: str
    signature_algorithm: Literal["ed25519"] = SIGNATURE_ALGORITHM
    signature: str

    @field_validator("content_id")
    @classmethod
    def validate_content_id(cls, value: str) -> str:
        if not CONTENT_ID_PATTERN.fullmatch(value):
            raise ValueError("content_id must use the sha256:<hex> format")
        return value

    @field_validator("signature")
    @classmethod
    def validate_signature_encoding(cls, value: str) -> str:
        decode_base64url(value, expected_bytes=64, label="signature")
        return value

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    def verify(self, *, at: datetime | None = None, require_active: bool = True) -> None:

        if self.content_id != self.pack.content_id:
            raise AlphaPackError("Alpha pack content ID does not match its content")
        public_key = Ed25519PublicKey.from_public_bytes(
            decode_base64url(
                self.pack.owner.public_key,
                expected_bytes=32,
                label="owner public_key",
            )
        )
        try:
            public_key.verify(
                decode_base64url(self.signature, expected_bytes=64, label="signature"),
                signature_domain(MESSAGE_TYPE) + self.pack.canonical_bytes,
            )
        except InvalidSignature as error:
            raise AlphaPackError("Alpha pack signature is invalid") from error
        if require_active and not self.pack.is_active(at):
            raise AlphaPackError("Alpha pack is not active at the requested time")

    @classmethod
    def from_json(cls, data: bytes | str) -> SignedAlphaPack:
        wire_bytes = data.encode("utf-8") if isinstance(data, str) else data
        return cls.from_wire_bytes(wire_bytes)

    def to_wire_bytes(self) -> bytes:
        wire_bytes = self.canonical_bytes
        if len(wire_bytes) > MAX_SIGNED_PACK_BYTES:
            raise AlphaPackError("Signed alpha pack exceeds the safe input size")
        return wire_bytes

    @classmethod
    def from_wire_bytes(cls, wire_bytes: bytes) -> SignedAlphaPack:
        if len(wire_bytes) > MAX_SIGNED_PACK_BYTES:
            raise AlphaPackError("Signed alpha pack exceeds the safe input size")
        value = cls.model_validate_json(wire_bytes)
        if value.to_wire_bytes() != wire_bytes:
            raise AlphaPackError("Signed alpha pack JSON is not in canonical wire form")
        return value


def sign_alpha_pack(pack: AlphaPack, private_key: Ed25519PrivateKey) -> SignedAlphaPack:

    if public_key_text(private_key) != pack.owner.public_key:
        raise AlphaPackError("Signing key does not match alpha pack owner")
    signature = private_key.sign(signature_domain(MESSAGE_TYPE) + pack.canonical_bytes)
    return SignedAlphaPack(
        pack=pack,
        content_id=pack.content_id,
        signature=encode_base64url(signature),
    )
