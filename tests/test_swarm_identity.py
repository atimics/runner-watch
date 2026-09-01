import os
from pathlib import Path

import pytest

from runner_swarm.identity import (
    NodeKeyError,
    load_or_create_node_key,
    private_key_from_text,
    private_key_text,
)
from runner_swarm.protocol import public_key_text


def test_node_key_is_stable_and_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "private" / "node.key"

    first = load_or_create_node_key(path)
    second = load_or_create_node_key(path)

    assert public_key_text(first) == public_key_text(second)
    assert path.stat().st_mode & 0o777 == 0o600
    assert private_key_from_text(path.read_text()).private_bytes_raw() == first.private_bytes_raw()


def test_node_key_rejects_open_permissions(tmp_path: Path) -> None:
    path = tmp_path / "node.key"
    path.write_text(private_key_text(load_or_create_node_key(tmp_path / "source.key")))
    os.chmod(path, 0o644)

    with pytest.raises(NodeKeyError, match="permissions"):
        load_or_create_node_key(path)


def test_node_key_rejects_links(tmp_path: Path) -> None:
    source = tmp_path / "source.key"
    load_or_create_node_key(source)
    link = tmp_path / "linked.key"
    link.symlink_to(source)

    with pytest.raises(NodeKeyError, match="regular file"):
        load_or_create_node_key(link)


def test_node_key_rejects_invalid_text() -> None:
    with pytest.raises(NodeKeyError, match="canonical Ed25519"):
        private_key_from_text("not-a-key")
