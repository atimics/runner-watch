from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import Field, TypeAdapter, field_validator, model_validator

from runner_swarm.protocol import (
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

MESSAGE_TYPE = "rati.signed_claim"

MAX_CLOCK_SKEW = timedelta(minutes=5)
MAX_OBSERVATION_AGE = timedelta(hours=24)
MAX_OBSERVATION_TTL = timedelta(hours=24)
MAX_RETRACTION_TTL = timedelta(days=7)
MAX_CLAIM_BYTES = 24 * 1024
MAX_WIRE_BYTES = 32 * 1024

CLAIM_ID_PATTERN = r"^sha256:[0-9a-f]{64}$"
SLUG_PATTERN = r"^[a-z0-9][a-z0-9._:/-]*$"
INSTRUMENT_PATTERN = r"^[A-Z0-9][A-Z0-9._:/-]*$"

ClaimId = Annotated[str, Field(pattern=CLAIM_ID_PATTERN)]


class ClaimVerificationError(ValueError):
    """The claim cannot be trusted as a valid signed protocol message."""


class ClaimExpiredError(ClaimVerificationError):
    """The claim was signed correctly but is no longer current."""


class TradeState(StrEnum):
    WATCH = "WATCH"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    MANAGE = "MANAGE"
    AVOID = "AVOID"
    EXIT = "EXIT"


class RiskLevel(StrEnum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    GUARDED = "GUARDED"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RiskSeverity(StrEnum):
    WARNING = "warning"
    HARD = "hard"


def _utc(value: datetime) -> datetime:
    return normalize_utc(value, field_name="claim timestamp")


def encode_public_key(public_key: Ed25519PublicKey) -> str:
    """Return the registry-free wire identity for an Ed25519 public key."""

    return public_key_text(public_key)


class EvidenceReferenceV1(SwarmModel):
    """A content reference, not a copy of provider data."""

    evidence_id: Annotated[str, Field(pattern=CLAIM_ID_PATTERN)]
    family: Annotated[str, Field(min_length=1, max_length=48, pattern=SLUG_PATTERN)]
    source: Annotated[str, Field(min_length=1, max_length=96, pattern=SLUG_PATTERN)]
    observed_at: datetime
    locator: Annotated[str, Field(min_length=1, max_length=512)] | None = None

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return _utc(value)


class SourceVersionV1(SwarmModel):
    family: Annotated[str, Field(min_length=1, max_length=48, pattern=SLUG_PATTERN)]
    source: Annotated[str, Field(min_length=1, max_length=96, pattern=SLUG_PATTERN)]
    version: Annotated[str, Field(min_length=1, max_length=64)]


class RiskVetoV1(SwarmModel):
    code: Annotated[str, Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)]
    reason: Annotated[str, Field(min_length=1, max_length=280)]
    severity: RiskSeverity = RiskSeverity.HARD
    evidence_ids: Annotated[tuple[ClaimId, ...], Field(max_length=8)] = ()

    @field_validator("evidence_ids")
    @classmethod
    def unique_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("A risk veto cannot repeat an evidence ID")
        return tuple(sorted(value))


class ClaimContentV1(SwarmModel):
    message_type: Literal["rati.signed_claim"] = MESSAGE_TYPE
    protocol_version: Literal["1"] = PROTOCOL_VERSION
    issuer_node_id: Annotated[str, Field(min_length=74, max_length=74)]
    issuer_public_key: Annotated[str, Field(min_length=43, max_length=43)]
    issued_at: datetime
    expires_at: datetime

    @field_validator("issuer_public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        decode_base64url(value, expected_bytes=32, label="issuer_public_key")
        return value

    @field_validator("issuer_node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("issuer_node_id must use the rati-node:<sha256> format")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def normalize_times(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_expiry_order(self) -> Self:
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if self.issuer_node_id != node_id_from_public_key(self.issuer_public_key):
            raise ValueError("issuer_node_id does not match issuer_public_key")
        return self


class RunnerObservationV1(ClaimContentV1):
    kind: Literal["runner_observation"] = "runner_observation"
    instrument: Annotated[str, Field(min_length=1, max_length=64, pattern=INSTRUMENT_PATTERN)]
    observed_at: datetime
    scanner_version: Annotated[str, Field(min_length=1, max_length=64)]
    schema_version: Annotated[str, Field(min_length=1, max_length=64)]
    source_versions: Annotated[tuple[SourceVersionV1, ...], Field(min_length=1, max_length=32)]
    setup_score_milli: Annotated[int, Field(ge=0, le=100_000)]
    rug_score_milli: Annotated[int, Field(ge=0, le=100_000)] | None = None
    rug_level: RiskLevel = RiskLevel.UNKNOWN
    trade_state: TradeState
    state_reason: Annotated[str, Field(min_length=1, max_length=280)]
    signals: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=120)], ...],
        Field(max_length=32),
    ] = ()
    risk_vetoes: Annotated[tuple[RiskVetoV1, ...], Field(max_length=16)] = ()
    evidence: Annotated[tuple[EvidenceReferenceV1, ...], Field(min_length=1, max_length=32)]
    supersedes_claim_id: ClaimId | None = None

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("signals")
    @classmethod
    def unique_signals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("A runner observation cannot repeat a signal")
        return tuple(sorted(value))

    @field_validator("source_versions")
    @classmethod
    def sort_source_versions(
        cls, value: tuple[SourceVersionV1, ...]
    ) -> tuple[SourceVersionV1, ...]:
        return tuple(sorted(value, key=lambda item: (item.family, item.source, item.version)))

    @field_validator("evidence")
    @classmethod
    def sort_evidence(
        cls, value: tuple[EvidenceReferenceV1, ...]
    ) -> tuple[EvidenceReferenceV1, ...]:
        return tuple(sorted(value, key=lambda item: item.evidence_id))

    @field_validator("risk_vetoes")
    @classmethod
    def sort_risk_vetoes(cls, value: tuple[RiskVetoV1, ...]) -> tuple[RiskVetoV1, ...]:
        return tuple(sorted(value, key=lambda item: item.code))

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.expires_at - self.issued_at > MAX_OBSERVATION_TTL:
            raise ValueError("A runner observation cannot live longer than 24 hours")
        if self.observed_at > self.issued_at + MAX_CLOCK_SKEW:
            raise ValueError("observed_at is too far after issued_at")
        if self.observed_at < self.issued_at - MAX_OBSERVATION_AGE:
            raise ValueError("observed_at cannot be more than 24 hours before issued_at")
        if self.observed_at >= self.expires_at:
            raise ValueError("observed_at must be earlier than expires_at")

        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("A runner observation cannot repeat an evidence ID")

        source_keys = [(item.family, item.source) for item in self.source_versions]
        if len(set(source_keys)) != len(source_keys):
            raise ValueError("A runner observation cannot repeat a source version")

        veto_codes = [item.code for item in self.risk_vetoes]
        if len(set(veto_codes)) != len(veto_codes):
            raise ValueError("A runner observation cannot repeat a risk veto code")

        missing = {
            evidence_id
            for veto in self.risk_vetoes
            for evidence_id in veto.evidence_ids
            if evidence_id not in evidence_ids
        }
        if missing:
            raise ValueError("Risk vetoes must reference evidence included in the claim")
        if any(item.observed_at > self.issued_at + MAX_CLOCK_SKEW for item in self.evidence):
            raise ValueError("Evidence cannot be observed too far after issued_at")
        return self

    @property
    def has_hard_veto(self) -> bool:
        return any(veto.severity == RiskSeverity.HARD for veto in self.risk_vetoes)


class RetractionV1(ClaimContentV1):
    kind: Literal["retraction"] = "retraction"
    target_claim_id: ClaimId
    reason: Annotated[str, Field(min_length=1, max_length=280)]

    @model_validator(mode="after")
    def validate_retraction(self) -> Self:
        if self.expires_at - self.issued_at > MAX_RETRACTION_TTL:
            raise ValueError("A retraction cannot live longer than 7 days")
        return self


ClaimPayloadV1 = Annotated[RunnerObservationV1 | RetractionV1, Field(discriminator="kind")]
_CLAIM_ADAPTER = TypeAdapter(ClaimPayloadV1)


class SignedClaimV1(SwarmModel):
    """A signed, untrusted peer statement with a content-derived identity."""

    claim: ClaimPayloadV1
    claim_id: ClaimId
    signature_algorithm: Literal["ed25519"] = SIGNATURE_ALGORITHM
    signature: Annotated[str, Field(min_length=86, max_length=86)]

    @field_validator("signature")
    @classmethod
    def validate_signature_encoding(cls, value: str) -> str:
        decode_base64url(value, expected_bytes=64, label="signature")
        return value

    @model_validator(mode="after")
    def validate_size(self) -> Self:
        if len(self.claim_bytes()) > MAX_CLAIM_BYTES:
            raise ValueError("Claim content is too large")
        if len(canonical_json_bytes(self)) > MAX_WIRE_BYTES:
            raise ValueError("Signed claim is too large")
        return self

    @classmethod
    def sign(
        cls,
        claim: RunnerObservationV1 | RetractionV1,
        private_key: Ed25519PrivateKey,
    ) -> SignedClaimV1:
        claim = _CLAIM_ADAPTER.validate_python(claim.model_dump(mode="python"))
        expected_public_key = encode_public_key(private_key.public_key())
        if claim.issuer_public_key != expected_public_key:
            raise ValueError("The private key does not match issuer_public_key")
        claim_bytes = canonical_json_bytes(claim)
        if len(claim_bytes) > MAX_CLAIM_BYTES:
            raise ValueError("Claim content is too large")
        claim_id = _claim_id(claim_bytes)
        signature = private_key.sign(signature_domain(MESSAGE_TYPE) + claim_bytes)
        return cls(
            claim=claim,
            claim_id=claim_id,
            signature=encode_base64url(signature),
        )

    @classmethod
    def from_wire_bytes(cls, wire_bytes: bytes) -> SignedClaimV1:
        if len(wire_bytes) > MAX_WIRE_BYTES:
            raise ValueError("Signed claim is too large")
        try:
            wire_text = wire_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Signed claim must be UTF-8 JSON") from error
        value = cls.model_validate_json(wire_text)
        if value.to_wire_bytes() != wire_bytes:
            raise ValueError("Signed claim JSON is not in canonical wire form")
        return value

    def claim_bytes(self) -> bytes:
        return canonical_json_bytes(self.claim)

    def expected_claim_id(self) -> str:
        return _claim_id(self.claim_bytes())

    def to_wire_bytes(self) -> bytes:
        wire_bytes = canonical_json_bytes(self)
        if len(wire_bytes) > MAX_WIRE_BYTES:
            raise ValueError("Signed claim is too large")
        return wire_bytes

    def is_expired(self, at: datetime | None = None) -> bool:
        checked_at = _utc(at or datetime.now(UTC))
        return checked_at >= self.claim.expires_at

    def verify(self, *, at: datetime | None = None, require_current: bool = True) -> Self:
        self._verify_authenticity()

        checked_at = _utc(at or datetime.now(UTC))
        if self.claim.issued_at > checked_at + MAX_CLOCK_SKEW:
            raise ClaimVerificationError("issued_at is too far in the future")
        if require_current and self.is_expired(checked_at):
            raise ClaimExpiredError("The signed claim has expired")
        return self

    def _verify_authenticity(self) -> None:
        if self.claim_id != self.expected_claim_id():
            raise ClaimVerificationError("claim_id does not match the claim content")
        try:
            public_key_bytes = decode_base64url(
                self.claim.issuer_public_key,
                expected_bytes=32,
                label="issuer_public_key",
            )
            public_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)
            signature = decode_base64url(self.signature, expected_bytes=64, label="signature")
            public_key.verify(
                signature,
                signature_domain(MESSAGE_TYPE) + self.claim_bytes(),
            )
        except (InvalidSignature, ValueError) as error:
            raise ClaimVerificationError("Ed25519 signature verification failed") from error

    def verify_signature(self) -> bool:
        try:
            self._verify_authenticity()
        except ClaimVerificationError:
            return False
        return True

    def retracts(self, other: SignedClaimV1, *, at: datetime | None = None) -> bool:
        if not isinstance(self.claim, RetractionV1):
            return False
        try:
            self.verify(at=at)
            other.verify(at=at, require_current=False)
        except ClaimVerificationError:
            return False
        return (
            self.claim.target_claim_id == other.claim_id
            and self.claim.issuer_public_key == other.claim.issuer_public_key
        )

    def supersedes(self, other: SignedClaimV1, *, at: datetime | None = None) -> bool:
        if not isinstance(self.claim, RunnerObservationV1):
            return False
        try:
            self.verify(at=at)
            other.verify(at=at, require_current=False)
        except ClaimVerificationError:
            return False
        return (
            self.claim.supersedes_claim_id == other.claim_id
            and self.claim.issuer_public_key == other.claim.issuer_public_key
            and isinstance(other.claim, RunnerObservationV1)
            and self.claim.instrument == other.claim.instrument
            and self.claim.issued_at > other.claim.issued_at
        )


def _claim_id(claim_bytes: bytes) -> str:
    return content_id(claim_bytes)
