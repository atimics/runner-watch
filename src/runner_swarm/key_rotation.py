"""Dual-signed node-key continuity with explicit local decisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import Field, field_validator, model_validator

from runner_swarm.protocol import (
    CONTENT_ID_PATTERN,
    NODE_ID_PATTERN,
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

MESSAGE_TYPE = "rati.key_rotation"
MAX_ROTATION_LIFETIME = timedelta(days=30)
MAX_ROTATION_BYTES = 12 * 1024
MAX_SIGNED_ROTATION_BYTES = 16 * 1024
MAX_CLOCK_SKEW = timedelta(minutes=5)


class KeyRotationError(ValueError):
    """A rotation artifact or local continuity decision is invalid."""


class KeyRotationV1(SwarmModel):
    """An old identity's request to continue as a new identity."""

    message_type: Literal["rati.key_rotation"] = MESSAGE_TYPE
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    old_identity: NodeIdentity
    new_identity: NodeIdentity
    sequence: Annotated[int, Field(ge=1, le=2_147_483_647)]
    issued_at: datetime
    effective_at: datetime
    expires_at: datetime
    reason: Annotated[str, Field(min_length=1, max_length=280)]

    @field_validator("issued_at", "effective_at", "expires_at")
    @classmethod
    def normalize_times(cls, value: datetime, info: object) -> datetime:
        return normalize_utc(value, field_name=getattr(info, "field_name", "timestamp"))

    @model_validator(mode="after")
    def validate_rotation(self) -> Self:
        if self.old_identity.node_id == self.new_identity.node_id:
            raise ValueError("A key rotation must change the node identity")
        if self.effective_at < self.issued_at:
            raise ValueError("effective_at cannot be earlier than issued_at")
        if self.expires_at <= self.effective_at:
            raise ValueError("expires_at must be later than effective_at")
        if self.expires_at - self.issued_at > MAX_ROTATION_LIFETIME:
            raise ValueError("A key rotation cannot be valid for more than 30 days")
        if len(self.canonical_bytes) > MAX_ROTATION_BYTES:
            raise ValueError("Key rotation content is too large")
        return self

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self)

    @property
    def content_id(self) -> str:
        return content_id(self.canonical_bytes)


class SignedKeyRotationV1(SwarmModel):
    """A rotation countersigned by both the retiring and replacement keys."""

    rotation: KeyRotationV1
    content_id: Annotated[str, Field(min_length=71, max_length=71)]
    signature_algorithm: Literal["ed25519"] = SIGNATURE_ALGORITHM
    old_signature: Annotated[str, Field(min_length=86, max_length=86)]
    new_signature: Annotated[str, Field(min_length=86, max_length=86)]

    @field_validator("content_id")
    @classmethod
    def validate_content_id(cls, value: str) -> str:
        if not CONTENT_ID_PATTERN.fullmatch(value):
            raise ValueError("content_id must use the sha256:<hex> format")
        return value

    @field_validator("old_signature", "new_signature")
    @classmethod
    def validate_signature(cls, value: str) -> str:
        decode_base64url(value, expected_bytes=64, label="signature")
        return value

    @classmethod
    def sign(
        cls,
        rotation: KeyRotationV1,
        old_private_key: Ed25519PrivateKey,
        new_private_key: Ed25519PrivateKey,
    ) -> SignedKeyRotationV1:
        if public_key_text(old_private_key) != rotation.old_identity.public_key:
            raise ValueError("old_private_key does not match old_identity")
        if public_key_text(new_private_key) != rotation.new_identity.public_key:
            raise ValueError("new_private_key does not match new_identity")
        signed_bytes = signature_domain(MESSAGE_TYPE) + rotation.canonical_bytes
        return cls(
            rotation=rotation,
            content_id=rotation.content_id,
            old_signature=encode_base64url(old_private_key.sign(signed_bytes)),
            new_signature=encode_base64url(new_private_key.sign(signed_bytes)),
        )

    @classmethod
    def from_wire_bytes(cls, wire_bytes: bytes) -> SignedKeyRotationV1:
        if len(wire_bytes) > MAX_SIGNED_ROTATION_BYTES:
            raise ValueError("Signed key rotation is too large")
        try:
            value = cls.model_validate_json(wire_bytes)
        except UnicodeDecodeError as error:
            raise ValueError("Signed key rotation must be UTF-8 JSON") from error
        if value.to_wire_bytes() != wire_bytes:
            raise ValueError("Signed key rotation JSON is not in canonical wire form")
        return value

    def to_wire_bytes(self) -> bytes:
        wire_bytes = canonical_json_bytes(self)
        if len(wire_bytes) > MAX_SIGNED_ROTATION_BYTES:
            raise ValueError("Signed key rotation is too large")
        return wire_bytes

    def verify(
        self,
        *,
        at: datetime | None = None,
        require_current: bool = True,
    ) -> Self:
        if self.content_id != self.rotation.content_id:
            raise KeyRotationError("Rotation content ID does not match its payload")
        signed_bytes = signature_domain(MESSAGE_TYPE) + self.rotation.canonical_bytes
        try:
            _public_key(self.rotation.old_identity).verify(
                decode_base64url(self.old_signature, expected_bytes=64, label="old_signature"),
                signed_bytes,
            )
            _public_key(self.rotation.new_identity).verify(
                decode_base64url(self.new_signature, expected_bytes=64, label="new_signature"),
                signed_bytes,
            )
        except (InvalidSignature, ValueError) as error:
            raise KeyRotationError("Key rotation signatures are invalid") from error

        checked_at = normalize_utc(at or datetime.now(UTC), field_name="at")
        if self.rotation.issued_at > checked_at + MAX_CLOCK_SKEW:
            raise KeyRotationError("Key rotation was issued too far in the future")
        if require_current and checked_at >= self.rotation.expires_at:
            raise KeyRotationError("Key rotation has expired")
        return self


class RotationDecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVOKED = "revoked"


class LocalRotationDecision(SwarmModel):
    """A local trust decision; it is deliberately not a swarm wire artifact."""

    rotation_content_id: Annotated[str, Field(min_length=71, max_length=71)]
    old_node_id: Annotated[str, Field(min_length=74, max_length=74)]
    new_node_id: Annotated[str, Field(min_length=74, max_length=74)]
    sequence: Annotated[int, Field(ge=1)]
    status: RotationDecisionStatus
    decided_at: datetime
    reason: Annotated[str, Field(min_length=1, max_length=280)]

    @field_validator("rotation_content_id")
    @classmethod
    def validate_content_id(cls, value: str) -> str:
        if not CONTENT_ID_PATTERN.fullmatch(value):
            raise ValueError("rotation_content_id must use the sha256:<hex> format")
        return value

    @field_validator("old_node_id", "new_node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("Rotation decisions require a valid node ID")
        return value

    @field_validator("decided_at")
    @classmethod
    def normalize_decided_at(cls, value: datetime) -> datetime:
        return normalize_utc(value, field_name="decided_at")


class LocalKeyRotationRegistry:
    """Explicit local old-to-new mappings with reject and revoke history."""

    def __init__(self) -> None:
        self._artifacts: dict[str, SignedKeyRotationV1] = {}
        self._history: list[LocalRotationDecision] = []
        self._latest: dict[str, LocalRotationDecision] = {}

    @property
    def history(self) -> tuple[LocalRotationDecision, ...]:
        return tuple(self._history)

    def decision(self, rotation_content_id: str) -> LocalRotationDecision | None:
        return self._latest.get(rotation_content_id)

    def accept(
        self,
        signed: SignedKeyRotationV1,
        *,
        decided_at: datetime,
        reason: str,
    ) -> LocalRotationDecision:
        signed.verify(at=decided_at)
        rotation = signed.rotation
        decided_at = normalize_utc(decided_at, field_name="decided_at")
        if decided_at < rotation.effective_at:
            raise KeyRotationError("A rotation cannot be accepted before effective_at")
        active = self._active_decision_for_old(rotation.old_identity.node_id)
        if active is not None and active.rotation_content_id != signed.content_id:
            raise KeyRotationError("The old identity already has an accepted rotation")
        if self.resolve(rotation.new_identity.node_id) == rotation.old_identity.node_id:
            raise KeyRotationError("Accepting this rotation would create an identity cycle")
        conflicting_sequences = [
            decision
            for decision in self._history
            if decision.old_node_id == rotation.old_identity.node_id
            and decision.rotation_content_id != signed.content_id
            and decision.sequence >= rotation.sequence
        ]
        if conflicting_sequences:
            raise KeyRotationError("Rotation sequence must be newer than prior local decisions")
        self._artifacts[signed.content_id] = signed
        return self._record(signed, RotationDecisionStatus.ACCEPTED, decided_at, reason)

    def reject(
        self,
        signed: SignedKeyRotationV1,
        *,
        decided_at: datetime,
        reason: str,
    ) -> LocalRotationDecision:
        signed.verify(at=decided_at, require_current=False)
        decided_at = normalize_utc(decided_at, field_name="decided_at")
        active = self._active_decision_for_old(signed.rotation.old_identity.node_id)
        if active is not None and active.rotation_content_id == signed.content_id:
            raise KeyRotationError("Revoke an accepted rotation instead of rejecting it")
        self._artifacts[signed.content_id] = signed
        return self._record(signed, RotationDecisionStatus.REJECTED, decided_at, reason)

    def revoke(
        self,
        rotation_content_id: str,
        *,
        decided_at: datetime,
        reason: str,
    ) -> LocalRotationDecision:
        decided_at = normalize_utc(decided_at, field_name="decided_at")
        latest = self._latest.get(rotation_content_id)
        if latest is None or latest.status != RotationDecisionStatus.ACCEPTED:
            raise KeyRotationError("Only an accepted rotation can be revoked")
        if decided_at < latest.decided_at:
            raise KeyRotationError("A revocation cannot predate its acceptance")
        signed = self._artifacts[rotation_content_id]
        return self._record(signed, RotationDecisionStatus.REVOKED, decided_at, reason)

    def resolve(self, node_id: str) -> str:
        """Follow active local mappings to the current identity."""

        current = node_id
        seen: set[str] = set()
        while current not in seen:
            seen.add(current)
            decision = self._active_decision_for_old(current)
            if decision is None:
                return current
            current = decision.new_node_id
        raise KeyRotationError("Local key rotation mappings contain a cycle")

    def is_continuation(self, original_node_id: str, presented_node_id: str) -> bool:
        return self.resolve(original_node_id) == self.resolve(presented_node_id)

    def continuity_node_ids(self, node_id: str) -> tuple[str, ...]:
        """Return every identity connected by currently accepted rotations."""

        active_forward = {
            decision.old_node_id: decision.new_node_id
            for decision in self._latest.values()
            if decision.status == RotationDecisionStatus.ACCEPTED
        }
        current = node_id
        seen_forward: set[str] = set()
        while current in active_forward:
            if current in seen_forward:
                raise KeyRotationError("Local key rotation mappings contain a cycle")
            seen_forward.add(current)
            current = active_forward[current]

        predecessors: dict[str, set[str]] = {}
        for old_node_id, new_node_id in active_forward.items():
            predecessors.setdefault(new_node_id, set()).add(old_node_id)
        continuity = {current}
        pending = [current]
        while pending:
            descendant = pending.pop()
            for predecessor in predecessors.get(descendant, ()):
                if predecessor not in continuity:
                    continuity.add(predecessor)
                    pending.append(predecessor)
        return tuple(sorted(continuity))

    def _active_decision_for_old(self, old_node_id: str) -> LocalRotationDecision | None:
        candidates = (
            decision
            for decision in self._latest.values()
            if decision.old_node_id == old_node_id
            and decision.status == RotationDecisionStatus.ACCEPTED
        )
        return next(candidates, None)

    def _record(
        self,
        signed: SignedKeyRotationV1,
        status: RotationDecisionStatus,
        decided_at: datetime,
        reason: str,
    ) -> LocalRotationDecision:
        rotation = signed.rotation
        decision = LocalRotationDecision(
            rotation_content_id=signed.content_id,
            old_node_id=rotation.old_identity.node_id,
            new_node_id=rotation.new_identity.node_id,
            sequence=rotation.sequence,
            status=status,
            decided_at=decided_at,
            reason=reason,
        )
        self._history.append(decision)
        self._latest[signed.content_id] = decision
        return decision


def _public_key(identity: NodeIdentity) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(
        decode_base64url(identity.public_key, expected_bytes=32, label="public_key")
    )
