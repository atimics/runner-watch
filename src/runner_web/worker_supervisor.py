from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable

LOG = logging.getLogger(__name__)


class WorkerWatchdog:
    def __init__(self, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Worker heartbeat timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self._last_heartbeat = time.monotonic()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="worker-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def heartbeat(self) -> None:
        with self._lock:
            self._last_heartbeat = time.monotonic()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join()

    def _watch(self) -> None:
        while not self._stopped.wait(min(1.0, self.timeout_seconds / 4)):
            with self._lock:
                age = time.monotonic() - self._last_heartbeat
            if age >= self.timeout_seconds:
                LOG.critical("Worker heartbeat stalled for %.1f seconds; restarting process", age)
                os._exit(1)


def run_supervised(
    worker: Callable[[Callable[[], None]], Awaitable[None]], *, timeout_seconds: float
) -> None:
    watchdog = WorkerWatchdog(timeout_seconds)
    watchdog.start()
    try:
        asyncio.run(worker(watchdog.heartbeat))
    except BaseException:
        LOG.exception("Worker stopped after an error; restarting process")
        os._exit(1)
    else:
        watchdog.stop()
