from pathlib import Path

import pytest

from runner_swarm.config import DEFAULT_TOPIC, SwarmMode, SwarmRuntimeConfig
from runner_watch import __version__


def test_default_runtime_is_solo_and_local_first() -> None:
    config = SwarmRuntimeConfig.from_env({})

    assert config.mode == SwarmMode.SOLO
    assert config.software_version == __version__
    assert config.attached is False
    assert config.bootstrap_urls == ()
    assert config.topics == (DEFAULT_TOPIC,)
    assert config.key_path == Path("data/swarm/node.key")
    assert config.node_private_key_text is None
    assert config.local_trust_store_path == Path("data/swarm/local-trust.db")
    assert config.alpha_pack_path is None
    assert config.claim_schema_versions == ("runner-v1",)
    assert config.max_claims_per_scan == 10
    assert config.publish_scan_claims is False


def test_attached_runtime_accepts_bounded_https_bootstraps() -> None:
    config = SwarmRuntimeConfig.from_env(
        {
            "SWARM_MODE": "attached",
            "SWARM_BOOTSTRAP_URLS": "https://seed.example,https://pack.example/swarm",
            "SWARM_TOPICS": "markets/equities/us/runners,markets/equities/us/halts",
            "SWARM_ALLOW_PRIVATE_BOOTSTRAP": "true",
            "SWARM_ALPHA_PACK_PATH": "/run/secrets/default-pack.json",
            "SWARM_CLAIM_SCHEMA_VERSIONS": "runner-v1,runner-v2",
            "SWARM_PUBLISH_SCANS": "true",
        }
    )

    assert config.attached is True
    assert config.allow_private_bootstrap is True
    assert config.bootstrap_urls == (
        "https://seed.example",
        "https://pack.example/swarm",
    )
    assert config.alpha_pack_path == Path("/run/secrets/default-pack.json")
    assert config.claim_schema_versions == ("runner-v1", "runner-v2")
    assert config.publish_scan_claims is True


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("SWARM_MODE", "mesh", "solo or attached"),
        ("SWARM_BOOTSTRAP_URLS", "http://seed.example", "https"),
        ("SWARM_BOOTSTRAP_URLS", "https://user:secret@seed.example", "credentials"),
        ("SWARM_PUBLIC_URL", "https://node.example/swarm", "origin"),
        ("SWARM_PEER_RATE_LIMIT_PER_MINUTE", "0", "between"),
        ("SWARM_ALLOW_PRIVATE_BOOTSTRAP", "maybe", "true or false"),
        ("SWARM_CLAIM_SCHEMA_VERSIONS", "", "between 1 and 32"),
        ("SWARM_MAX_CLAIMS_PER_SCAN", "0", "between"),
        ("SWARM_PUBLISH_SCANS", "sometimes", "true or false"),
    ],
)
def test_runtime_rejects_unsafe_or_invalid_environment(name: str, value: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SwarmRuntimeConfig.from_env({name: value})


def test_attached_fly_runtime_requires_one_shared_secret_identity() -> None:
    with pytest.raises(ValueError, match="SWARM_NODE_PRIVATE_KEY"):
        SwarmRuntimeConfig.from_env(
            {
                "FLY_APP_NAME": "runner-watch",
                "SWARM_MODE": "attached",
            }
        )

    config = SwarmRuntimeConfig.from_env(
        {
            "FLY_APP_NAME": "runner-watch",
            "SWARM_MODE": "attached",
            "SWARM_NODE_PRIVATE_KEY": "shared-secret-value",
        }
    )
    assert config.node_private_key_text == "shared-secret-value"
