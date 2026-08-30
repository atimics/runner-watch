from pathlib import Path

import pytest

from runner_swarm.config import DEFAULT_TOPIC, SwarmMode, SwarmRuntimeConfig


def test_default_runtime_is_solo_and_local_first() -> None:
    config = SwarmRuntimeConfig.from_env({})

    assert config.mode == SwarmMode.SOLO
    assert config.attached is False
    assert config.bootstrap_urls == ()
    assert config.topics == (DEFAULT_TOPIC,)
    assert config.key_path == Path("data/swarm/node.key")


def test_attached_runtime_accepts_bounded_https_bootstraps() -> None:
    config = SwarmRuntimeConfig.from_env(
        {
            "SWARM_MODE": "attached",
            "SWARM_BOOTSTRAP_URLS": "https://seed.example,https://pack.example/swarm",
            "SWARM_TOPICS": "markets/equities/us/runners,markets/equities/us/halts",
            "SWARM_ALLOW_PRIVATE_BOOTSTRAP": "true",
        }
    )

    assert config.attached is True
    assert config.allow_private_bootstrap is True
    assert config.bootstrap_urls == (
        "https://seed.example",
        "https://pack.example/swarm",
    )


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("SWARM_MODE", "mesh", "solo or attached"),
        ("SWARM_BOOTSTRAP_URLS", "http://seed.example", "https"),
        ("SWARM_BOOTSTRAP_URLS", "https://user:secret@seed.example", "credentials"),
        ("SWARM_PUBLIC_URL", "https://node.example/swarm", "origin"),
        ("SWARM_PEER_RATE_LIMIT_PER_MINUTE", "0", "between"),
        ("SWARM_ALLOW_PRIVATE_BOOTSTRAP", "maybe", "true or false"),
    ],
)
def test_runtime_rejects_unsafe_or_invalid_environment(
    name: str, value: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        SwarmRuntimeConfig.from_env({name: value})
