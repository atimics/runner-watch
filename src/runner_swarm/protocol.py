"""Shared wire rules for RATi swarm protocol messages."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import unicodedata
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROTOCOL_VERSION = "1"
SIGNATURE_ALGORITHM = "ed25519"
MAX_SAFE_INTEGER = 9_007_199_254_740_991

CONTENT_ID_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
NODE_ID_PATTERN = re.compile(r"^rati-node:[0-9a-f]{64}$")
_B64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class SwarmModel(BaseModel):
    """Immutable, closed model used at a swarm trust boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def normalize_utc(value: datetime, *, field_name: str = "timestamp") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)


def encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_base64url(value: str, *, expected_bytes: int, label: str) -> bytes:
    if not _B64URL_PATTERN.fullmatch(value):
        raise ValueError(f"{label} must be unpadded URL-safe base64")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"{label} must be unpadded URL-safe base64") from error
    if len(decoded) != expected_bytes:
        raise ValueError(f"{label} must encode exactly {expected_bytes} bytes")
    if not hmac.compare_digest(encode_base64url(decoded), value):
        raise ValueError(f"{label} must use canonical unpadded URL-safe base64")
    return decoded


def public_key_text(key: Ed25519PublicKey | Ed25519PrivateKey) -> str:
    if isinstance(key, Ed25519PrivateKey):
        key = key.public_key()
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return encode_base64url(raw)


def node_id_from_public_key(key: str | Ed25519PublicKey | Ed25519PrivateKey) -> str:
    if isinstance(key, str):
        raw = decode_base64url(key, expected_bytes=32, label="public_key")
    else:
        if isinstance(key, Ed25519PrivateKey):
            key = key.public_key()
        raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return f"rati-node:{hashlib.sha256(raw).hexdigest()}"


def content_id(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def signature_domain(message_type: str, protocol_version: str = PROTOCOL_VERSION) -> bytes:
    """Return the common, type-separated prefix for a signed payload."""

    if not message_type or "\x00" in message_type or "\x00" in protocol_version:
        raise ValueError("Signature domain values must be non-empty and cannot contain NUL")
    return (
        b"RATI-SWARM\x00"
        + message_type.encode("ascii")
        + b"\x00"
        + protocol_version.encode("ascii")
        + b"\x00"
    )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        normalized = normalize_utc(value)
        return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("Canonical JSON object keys must be strings")
            key = unicodedata.normalize("NFC", raw_key)
            if key in normalized:
                raise ValueError("Canonical JSON keys collide after Unicode normalization")
            normalized[key] = _canonical_value(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("Canonical JSON integers must fit the portable safe-integer range")
        return value
    if isinstance(value, float):
        raise ValueError("Signed swarm payloads must use scaled integers instead of floats")
    raise ValueError(f"Unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: BaseModel | dict[str, Any]) -> bytes:
    """Serialize the portable RATi canonical JSON profile."""

    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class NodeIdentity(SwarmModel):
    """A stable swarm node ID bound to one Ed25519 public key."""

    node_id: str
    public_key: str = Field(min_length=43, max_length=43)
    display_name: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        decode_base64url(value, expected_bytes=32, label="public_key")
        return value

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = unicodedata.normalize("NFC", value.strip())
        if not clean:
            raise ValueError("display_name cannot be empty")
        return clean

    @model_validator(mode="after")
    def bind_node_id_to_key(self) -> NodeIdentity:
        if not NODE_ID_PATTERN.fullmatch(self.node_id):
            raise ValueError("node_id must use the rati-node:<sha256> format")
        if self.node_id != node_id_from_public_key(self.public_key):
            raise ValueError("node_id does not match public_key")
        return self
