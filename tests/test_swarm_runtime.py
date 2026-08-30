from datetime import UTC, datetime
from pathlib import Path

import pytest

from runner_swarm.config import SwarmRuntimeConfig
from runner_swarm.node_manifest import verify_signed_node_manifest
from runner_swarm.runtime import SwarmNodeIdentity, build_signed_runtime_manifest

NOW = datetime(2026, 8, 30, 17, 12, 45, 123456, tzinfo=UTC)


def _config(tmp_path: Path, **overrides: str) -> SwarmRuntimeConfig:
    return SwarmRuntimeConfig.from_env(
        {
            "SWARM_KEY_PATH": str(tmp_path / "node.key"),
            "SWARM_PUBLIC_URL": "https://node.example",
            **overrides,
        }
    )


def test_runtime_identity_and_manifest_share_one_stable_key(tmp_path: Path) -> None:
    config = _config(tmp_path, SWARM_MODE="attached")
    first_identity = SwarmNodeIdentity.load(config)
    second_identity = SwarmNodeIdentity.load(config)

    signed = build_signed_runtime_manifest(config, first_identity, at=NOW)
    manifest = verify_signed_node_manifest(signed, at=NOW.replace(microsecond=0))

    assert first_identity.node_id == second_identity.node_id == manifest.node_id
    assert manifest.issued_at.microsecond == 0
    assert manifest.endpoints[0].address == "https://node.example/swarm/v1"
    assert {item.name for item in manifest.capabilities} == {
        "claims.publish",
        "claims.receive",
        "discovery.manifest",
    }
    assert manifest.supported_topics == config.topics


def test_runtime_manifest_requires_an_intentional_public_origin(tmp_path: Path) -> None:
    config = SwarmRuntimeConfig.from_env({"SWARM_KEY_PATH": str(tmp_path / "node.key")})
    identity = SwarmNodeIdentity.load(config)

    with pytest.raises(ValueError, match="SWARM_PUBLIC_URL"):
        build_signed_runtime_manifest(config, identity, at=NOW)
