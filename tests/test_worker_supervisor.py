import asyncio
import json
import subprocess
import sys
import textwrap
import threading

import pytest

from runner_web import main as web_main


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("time.sleep(60)", "Worker heartbeat stalled"),
        (
            "asyncio.create_task(asyncio.to_thread(time.sleep, 60))\n"
            "await asyncio.sleep(0.05)\n"
            "raise RuntimeError('database connection failed')",
            "Worker heartbeat stalled",
        ),
        (
            "threading.Thread(target=time.sleep, args=(60,)).start()\n"
            "raise RuntimeError('database connection failed')",
            "Worker stopped after an error",
        ),
    ],
)
def test_supervisor_exits_a_stalled_or_failed_process(body: str, message: str) -> None:
    source = (
        "import asyncio, threading, time\n"
        "from runner_web.worker_supervisor import run_supervised\n"
        "async def worker(heartbeat):\n"
        + textwrap.indent(body, "    ")
        + "\nrun_supervised(worker, timeout_seconds=0.5)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, timeout=8
    )

    assert result.returncode == 1
    assert message in result.stderr


def test_supervisor_accepts_heartbeats_and_stops_after_clean_shutdown() -> None:
    source = """
import asyncio
import time
from runner_web.worker_supervisor import run_supervised

async def worker(heartbeat):
    for _ in range(25):
        heartbeat()
        await asyncio.sleep(0.05)

run_supervised(worker, timeout_seconds=0.8)
time.sleep(1)
"""

    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True, timeout=8
    )

    assert result.returncode == 0, result.stderr


def test_heartbeat_waits_for_database_commit_while_other_tasks_run(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    writes = []
    pulses = []
    main_thread = threading.get_ident()

    def write(key, value):
        writes.append((key, json.loads(value), threading.get_ident()))
        started.set()
        assert release.wait(2)

    monkeypatch.setattr(web_main, "worker_state", write)
    monkeypatch.setattr(web_main, "_worker_heartbeat_detail", lambda workers: {"status": "ok"})
    monkeypatch.setattr(web_main, "redis_configured", lambda: False)

    async def exercise():
        pulsed = asyncio.Event()

        def heartbeat():
            pulses.append(True)
            pulsed.set()

        task = asyncio.create_task(web_main.worker_process_heartbeat([], heartbeat))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert pulses == []
            assert writes[0][2] != main_thread
            release.set()
            await asyncio.wait_for(pulsed.wait(), 1)
            assert pulses == [True]
        finally:
            release.set()
            await web_main._stop_tasks([task])

    asyncio.run(exercise())


@pytest.mark.parametrize("database_error", [False, True])
def test_heartbeat_requires_healthy_workers_and_a_successful_write(monkeypatch, database_error):
    pulses = []

    def write(key, value):
        if database_error:
            raise RuntimeError("database connection failed")

    monkeypatch.setattr(web_main, "worker_state", write)
    monkeypatch.setattr(
        web_main,
        "_worker_heartbeat_detail",
        lambda workers: {"status": "ok" if database_error else "degraded"},
    )
    monkeypatch.setattr(web_main, "redis_configured", lambda: False)

    async def exercise():
        task = asyncio.create_task(web_main.worker_process_heartbeat([], lambda: pulses.append(1)))
        if database_error:
            with pytest.raises(RuntimeError, match="database connection failed"):
                await task
        else:
            await asyncio.sleep(0.05)
            await web_main._stop_tasks([task])
        assert pulses == []

    asyncio.run(exercise())


def test_shutdown_finishes_all_tasks_after_a_worker_failure():
    async def exercise():
        finished = asyncio.Event()

        async def failed():
            raise RuntimeError("database connection failed")

        async def running():
            try:
                await asyncio.sleep(60)
            finally:
                finished.set()

        tasks = [asyncio.create_task(failed()), asyncio.create_task(running())]
        await asyncio.sleep(0)
        await web_main._stop_tasks(tasks)
        assert finished.is_set()
        assert all(task.done() for task in tasks)

    asyncio.run(exercise())
