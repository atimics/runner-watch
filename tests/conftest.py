from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.pycache_prefix = str(Path(__file__).resolve().parents[1] / ".test-cache.nosync" / "pycache")

# Starting the app lifespan in a test spawns the background workers, and several of them
# reach the network immediately and archive what they fetch into whatever database the
# current test has pointed at. Keep them off for the whole suite.
os.environ.setdefault("BACKGROUND_WORKERS_ENABLED", "0")

# A scan spawns the Telegram dispatch on a daemon thread, which writes through the same
# connection helper. Left on, a test that runs a scan could post outward and land rows in
# whichever database the next test points at.
os.environ.setdefault("TELEGRAM_RUNNER_ALERTS", "0")
os.environ.setdefault("TELEGRAM_MEMECOIN_ALERTS", "0")
os.environ.setdefault("TELEGRAM_RUNNER_REPORTS_PER_DAY", "0")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "")


def suspend_running_loop() -> Any:
    """Clear the loop installed on this thread and return it for restoration.

    Playwright's synchronous API calls ``asyncio._set_running_loop`` with its own
    private loop and keeps it set for the whole session. A synchronous unit test
    that calls ``asyncio.run()`` then fails with "cannot be called from a running
    event loop". Suspending the loop around non-browser tests lets both suites run
    in one process; browser tests keep the loop so Playwright keeps working.
    """
    previous = asyncio.events._get_running_loop()
    if previous is not None:
        asyncio.events._set_running_loop(None)
    return previous


def resume_running_loop(previous: Any) -> None:
    if previous is not None:
        asyncio.events._set_running_loop(previous)


def _is_browser_test(item: Any) -> bool:
    return item.get_closest_marker("browser") is not None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: Any):
    if _is_browser_test(item):
        yield
        return
    previous = suspend_running_loop()
    try:
        yield
    finally:
        resume_running_loop(previous)
