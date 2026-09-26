"""How much memory this process holds, and which job grew it.

The worker runs every collector in one Python process on a 1 GB machine, and
it was being OOM-killed every hour or two. A process listing only shows one
big python, so this measures resident memory around each threadpool job and
names the job that grew it. Python rarely hands memory back, so growth that
sticks to one job name is where to look.
"""

from __future__ import annotations

import logging
import os
import resource
import sys
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi.concurrency import run_in_threadpool as _run_in_threadpool

LOG = logging.getLogger(__name__)

GROWTH_LOG_MB = max(1.0, float(os.getenv("WORKER_MEMORY_GROWTH_LOG_MB", "25")))

T = TypeVar("T")


def rss_mb() -> float | None:
    """Current resident memory in MB, from /proc on Linux."""

    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return None


def peak_rss_mb() -> float:
    """The most resident memory this process has held at once."""

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS bytes.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def job_name(func: Callable[..., Any]) -> str:
    return f"{getattr(func, '__module__', '?')}.{getattr(func, '__qualname__', repr(func))}"


async def run_in_threadpool(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """FastAPI's run_in_threadpool, logging any job that grew the process.

    Jobs run side by side, so one line is a lead rather than proof; the same
    name growing the process again and again is the signal.
    """

    before = rss_mb()
    try:
        return await _run_in_threadpool(func, *args, **kwargs)
    finally:
        after = rss_mb()
        if before is not None and after is not None and after - before >= GROWTH_LOG_MB:
            LOG.warning(
                "memory_growth job=%s grew_mb=%.0f rss_mb=%.0f peak_mb=%.0f",
                job_name(func),
                after - before,
                after,
                peak_rss_mb(),
            )
