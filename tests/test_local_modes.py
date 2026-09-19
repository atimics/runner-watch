from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_local_compose_has_the_production_process_boundaries() -> None:
    config = yaml.safe_load((ROOT / "compose.local.yml").read_text())
    services = config["services"]

    assert {"database", "cache", "migrate", "web", "worker", "trainer"} <= services.keys()
    assert services["web"]["environment"]["PROCESS_ROLE"] == "web"
    assert services["worker"]["environment"]["PROCESS_ROLE"] == "worker"
    assert services["trainer"]["command"] == ["stonks-trainer"]
    assert services["web"]["environment"]["REQUIRE_DATABASE_URL"] == "1"
    assert services["web"]["environment"]["REQUIRE_RATE_LIMIT_HASH_KEY"] == "1"
