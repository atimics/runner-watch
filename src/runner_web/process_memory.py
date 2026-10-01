"""How much memory this process holds, and which job grew it.

The worker runs every collector in one Python process on a 1 GB machine, and
it was being OOM-killed every hour or two. A process listing only shows one
big python, so this measures resident memory around each threadpool job and
names the job that grew it. Python rarely hands memory back, so growth that
sticks to one job name is where to look.
"""

from __future__ import annotations

import ctypes
import logging
import os
import resource
import sys
import time
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi.concurrency import run_in_threadpool as _run_in_threadpool

LOG = logging.getLogger(__name__)

GROWTH_LOG_MB = max(1.0, float(os.getenv("WORKER_MEMORY_GROWTH_LOG_MB", "25")))
TREND_LOG_SECONDS = max(30.0, float(os.getenv("WORKER_MEMORY_TREND_SECONDS", "300")))

_last_trend_at: float | None = None

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


class _MallInfo2(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_size_t)
        for name in (
            "arena",
            "ordblks",
            "smblks",
            "hblks",
            "hblkhd",
            "usmblks",
            "fsmblks",
            "uordblks",
            "fordblks",
            "keepcost",
        )
    ]


def _libc() -> Any:
    """glibc, or None where there is none (macOS, musl)."""

    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.mallinfo2.restype = _MallInfo2
        libc.malloc_trim.argtypes = [ctypes.c_size_t]
        return libc
    except (OSError, AttributeError):
        return None


_LIBC = _libc()


def heap_mb() -> dict[str, float] | None:
    """glibc's view of the heap: in use, freed but kept, and large mmapped blocks.

    Freed-but-kept growing while in-use stays flat is fragmentation, not a leak.
    """

    if _LIBC is None:
        return None
    info = _LIBC.mallinfo2()
    return {
        "used": info.uordblks / 2**20,
        "free": info.fordblks / 2**20,
        "mmap": info.hblkhd / 2**20,
    }


def trim_heap() -> None:
    """Hand freed heap pages back to the system."""

    if _LIBC is not None:
        _LIBC.malloc_trim(0)


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


def log_memory_trend(*, clock: Callable[[], float] = time.monotonic) -> None:
    """Log resident memory every few minutes.

    A leak of a few MB a cycle never trips the per-job line above, but it
    still reaches the limit. A steady series of these lines shows the curve.
    """

    global _last_trend_at
    current = clock()
    if _last_trend_at is not None and current - _last_trend_at < TREND_LOG_SECONDS:
        return
    _last_trend_at = current
    before = rss_mb()
    heap = heap_mb()
    trim_heap()
    # Live Python objects: flat while resident memory climbs means freed memory
    # the allocator kept, not objects something holds.
    blocks = sys.getallocatedblocks()
    # Warning, not info: nothing configures logging, so the worker only prints
    # warnings and above, and an info line would never reach the Fly logs.
    LOG.warning(
        "memory_trend rss_mb=%s peak_mb=%.0f heap_used_mb=%s heap_free_mb=%s "
        "heap_mmap_mb=%s py_blocks=%d trimmed_rss_mb=%s",
        _rounded(before),
        peak_rss_mb(),
        _rounded(heap["used"] if heap else None),
        _rounded(heap["free"] if heap else None),
        _rounded(heap["mmap"] if heap else None),
        blocks,
        _rounded(rss_mb()),
    )


def _rounded(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.0f}"
