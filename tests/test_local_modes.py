from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_scanner_lab_is_clearly_separate_from_the_online_product() -> None:
    source = (ROOT / "app.py").read_text()

    assert 'page_title="RATi Scanner Lab"' in source
    assert "Scanner Lab is not the online RATi product" in source
    assert '"Stage (lab)"' in source
    assert "(0.20, 5.00)" in source


def test_local_compose_has_the_production_process_boundaries() -> None:
    config = yaml.safe_load((ROOT / "compose.local.yml").read_text())
    services = config["services"]

    assert {"database", "cache", "migrate", "web", "worker", "trainer"} <= services.keys()
    assert services["web"]["environment"]["PROCESS_ROLE"] == "web"
    assert services["worker"]["environment"]["PROCESS_ROLE"] == "worker"
    assert services["trainer"]["command"] == ["stonks-trainer"]
    assert services["web"]["environment"]["REQUIRE_DATABASE_URL"] == "1"
    assert services["web"]["environment"]["REQUIRE_RATE_LIMIT_HASH_KEY"] == "1"
