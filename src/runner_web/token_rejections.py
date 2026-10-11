"""Saved rejection receipts, using the SHA-256 history tree from RFC 9162."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def leaf_hash(value: str) -> str:
    return hashlib.sha256(b"\x00" + value.encode("utf-8")).hexdigest()


def _parent(left: str, right: str) -> str:
    return hashlib.sha256(b"\x01" + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def checkpoint(database: Any, size: int | None = None) -> dict[str, Any]:
    row = database.execute(
        "SELECT tree_size,root_hash,recorded_at FROM token_rejection_roots "
        + ("ORDER BY tree_size DESC LIMIT 1" if size is None else "WHERE tree_size=?"),
        () if size is None else (size,),
    ).fetchone()
    if row:
        return dict(row)
    if size not in (None, 0):
        raise ValueError("Receipt checkpoint unavailable")
    return {"tree_size": 0, "root_hash": hashlib.sha256(b"").hexdigest(), "recorded_at": None}


def _subtree(database: Any, start: int, size: int) -> str:
    if size & (size - 1) == 0:
        height = size.bit_length() - 1
        row = database.execute(
            "SELECT hash FROM token_rejection_nodes WHERE height=? AND node_index=?",
            (height, start >> height),
        ).fetchone()
        if row is None:
            raise ValueError("Receipt tree is incomplete")
        return str(row["hash"])
    split = 1 << (size.bit_length() - 1)
    return _parent(
        _subtree(database, start, split), _subtree(database, start + split, size - split)
    )


def append(database: Any, mint: str, leaf: dict[str, Any], evidence: Any, *, at: str) -> int:
    """Call inside the discovery write lock and transaction."""
    index = checkpoint(database)["tree_size"]
    encoded = canonical(leaf)
    database.execute(
        "INSERT INTO token_rejections(leaf_index,mint,leaf_json,evidence_json,recorded_at) "
        "VALUES(?,?,?,?,?)",
        (index, mint, encoded, canonical(evidence), at),
    )
    value, height, node = leaf_hash(encoded), 0, index
    while True:
        database.execute(
            "INSERT INTO token_rejection_nodes(height,node_index,hash) VALUES(?,?,?)",
            (height, node, value),
        )
        if node % 2 == 0:
            break
        left = database.execute(
            "SELECT hash FROM token_rejection_nodes WHERE height=? AND node_index=?",
            (height, node - 1),
        ).fetchone()["hash"]
        value, height, node = _parent(left, value), height + 1, node // 2
    size = index + 1
    database.execute(
        "INSERT INTO token_rejection_roots(tree_size,root_hash,recorded_at) VALUES(?,?,?)",
        (size, _subtree(database, 0, size), at),
    )
    return index


def proof(database: Any, index: int, size: int | None = None) -> dict[str, Any]:
    root = checkpoint(database, size)
    count = root["tree_size"]
    if not 0 <= index < count:
        raise ValueError("Receipt unavailable")
    row = database.execute(
        "SELECT leaf_json,evidence_json FROM token_rejections WHERE leaf_index=?", (index,)
    ).fetchone()

    def path(start: int, length: int, target: int) -> list[str]:
        if length == 1:
            return []
        split = 1 << ((length - 1).bit_length() - 1)
        if target < split:
            return path(start, split, target) + [_subtree(database, start + split, length - split)]
        return path(start + split, length - split, target - split) + [
            _subtree(database, start, split)
        ]

    return {
        "scheme": "rati-token-rejections-v1",
        "hash_algorithm": "sha256-rfc9162",
        **root,
        "leaf_index": index,
        "leaf_json": row["leaf_json"],
        "leaf_hash": leaf_hash(row["leaf_json"]),
        "inclusion_path": path(0, count, index),
        "evidence": json.loads(row["evidence_json"]),
    }


def verify(receipt: dict[str, Any]) -> bool:
    """Verify a receipt against its checkpoint. Keep the checkpoint for later comparison."""
    try:
        index, size = receipt["leaf_index"], receipt["tree_size"]
        if type(index) is not int or type(size) is not int or not 0 <= index < size:
            return False
        value = leaf_hash(receipt["leaf_json"])
        last = size - 1
        for sibling in receipt["inclusion_path"]:
            if last == 0 or len(sibling) != 64:
                return False
            if index & 1 or index == last:
                value = _parent(sibling, value)
                while index and index % 2 == 0:
                    index >>= 1
                    last >>= 1
            else:
                value = _parent(value, sibling)
            index >>= 1
            last >>= 1
        evidence_hash = hashlib.sha256(canonical(receipt["evidence"]).encode()).hexdigest()
        return (
            last == 0
            and value == receipt["root_hash"]
            and json.loads(receipt["leaf_json"])["evidence_sha256"] == evidence_hash
        )
    except (ValueError, TypeError, KeyError):
        return False
