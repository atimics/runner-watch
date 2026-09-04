from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from runner_swarm.protocol import decode_base64url, encode_base64url

PRIVATE_KEY_BYTES = 32


class NodeKeyError(ValueError):
    pass


def private_key_text(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return encode_base64url(raw)


def private_key_from_text(value: str) -> Ed25519PrivateKey:
    try:
        raw = decode_base64url(value.strip(), label="node private key", expected_bytes=32)
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise NodeKeyError("Node private key is not a canonical Ed25519 key") from exc


def _read_key(path: Path) -> Ed25519PrivateKey:
    try:
        details = path.lstat()
    except OSError as exc:
        raise NodeKeyError(f"Cannot inspect node key at {path}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise NodeKeyError("Node key path must be a regular file, not a link")
    if details.st_mode & 0o077:
        raise NodeKeyError("Node key file permissions must be 0600 or stricter")
    try:
        value = path.read_text(encoding="ascii")
    except OSError as exc:
        raise NodeKeyError(f"Cannot read node key at {path}") from exc
    return private_key_from_text(value)


def load_or_create_node_key(path: Path) -> Ed25519PrivateKey:

    path = Path(path)
    if path.exists() or path.is_symlink():
        return _read_key(path)

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_key = Ed25519PrivateKey.generate()
    payload = (private_key_text(private_key) + "\n").encode("ascii")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_key(path)
    except OSError as exc:
        raise NodeKeyError(f"Cannot create node key at {path}") from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise NodeKeyError(f"Cannot write node key at {path}") from exc
    return private_key
