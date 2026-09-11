from __future__ import annotations

import os
from pathlib import Path

from runner_web import main as web_main

ROOT = Path(__file__).parents[1]


def test_the_suite_runs_with_background_workers_disabled() -> None:

    assert os.environ["BACKGROUND_WORKERS_ENABLED"] == "0"
    assert web_main.BACKGROUND_WORKERS_ENABLED is False


def test_disabled_workers_start_nothing() -> None:

    assert web_main._start_worker_tasks() == []


def test_no_test_starts_the_application_lifespan() -> None:

    offenders = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("with TestClient("):
                continue
            if any(target in stripped for target in ("main.app", "web_main.app")):
                offenders.append(f"{path.name}:{number}")
    assert offenders == [], (
        "Using TestClient as a context manager runs the app lifespan, which starts the "
        "background workers; several reach the network and archive documents into whichever "
        f"database the current test has pointed at. Offenders: {offenders}"
    )


def test_network_reaching_workers_do_not_fire_on_the_boot_instant() -> None:

    source = (ROOT / "src/runner_web/main.py").read_text()
    for worker in ("edgar_worker", "trading_halt_worker", "outcome_worker"):
        start = source.find(f"async def {worker}() -> None:")
        if start < 0:
            continue
        body = source[start : start + 400]
        assert "await asyncio.sleep(" in body.split("while True:")[0], (
            f"{worker} reaches the network as soon as the process boots"
        )
