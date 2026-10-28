from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import pytest

from runner_web import process_memory


@pytest.fixture
def heavy_jobs(monkeypatch):
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(process_memory, "_HEAVY_JOB_EXECUTOR", executor)
        monkeypatch.setattr(process_memory, "trim_heap", lambda: None)
        yield


def test_large_jobs_share_a_slot_while_light_jobs_continue(heavy_jobs):
    started, release, second_started = (threading.Event() for _ in range(3))

    def first():
        started.set()
        assert release.wait(5)
        return "first"

    def second():
        second_started.set()
        return "second"

    async def exercise():
        first_task = asyncio.create_task(process_memory.run_memory_heavy_job(first))
        second_task = None
        try:
            assert await asyncio.to_thread(started.wait, 5)
            second_task = asyncio.create_task(process_memory.run_memory_heavy_job(second))
            assert await process_memory.run_in_threadpool(lambda: "fresh quote") == "fresh quote"
            assert not second_started.is_set()
        finally:
            release.set()
            assert await asyncio.wait_for(first_task, 5) == "first"
            if second_task is not None:
                assert await asyncio.wait_for(second_task, 5) == "second"

    asyncio.run(exercise())


def test_cancelled_running_job_keeps_its_slot_until_the_thread_finishes(heavy_jobs):
    started, release, second_started = (threading.Event() for _ in range(3))

    def first():
        started.set()
        assert release.wait(5)

    def second():
        second_started.set()

    async def exercise():
        first_task = asyncio.create_task(process_memory.run_memory_heavy_job(first))
        second_task = None
        try:
            assert await asyncio.to_thread(started.wait, 5)
            first_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_task
            second_task = asyncio.create_task(process_memory.run_memory_heavy_job(second))
            await asyncio.sleep(0)
            assert not second_started.is_set()
        finally:
            release.set()
            if second_task is not None:
                await asyncio.wait_for(second_task, 5)

    asyncio.run(exercise())


def test_cancelled_queued_job_is_skipped(heavy_jobs):
    started, release, cancelled_ran = (threading.Event() for _ in range(3))

    def first():
        started.set()
        assert release.wait(5)

    async def exercise():
        first_task = asyncio.create_task(process_memory.run_memory_heavy_job(first))
        try:
            assert await asyncio.to_thread(started.wait, 5)
            queued = asyncio.create_task(process_memory.run_memory_heavy_job(cancelled_ran.set))
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            await asyncio.sleep(0)
        finally:
            release.set()
            await asyncio.wait_for(first_task, 5)
        assert await process_memory.run_memory_heavy_job(lambda: "next") == "next"
        assert not cancelled_ran.is_set()

    asyncio.run(exercise())


def test_errors_release_the_slot_and_trim_freed_memory(heavy_jobs, monkeypatch):
    trimmed = []
    monkeypatch.setattr(process_memory, "trim_heap", lambda: trimmed.append(True))

    def fail():
        raise ValueError("job failed")

    def next_job(value, *, label):
        return label, value

    async def exercise():
        with pytest.raises(ValueError, match="job failed"):
            await process_memory.run_memory_heavy_job(fail)
        assert await process_memory.run_memory_heavy_job(next_job, 7, label="done") == (
            "done",
            7,
        )

    asyncio.run(exercise())
    assert trimmed == [True, True]


def test_jobs_receive_the_callers_context_and_keep_changes_local(heavy_jobs):
    receipt = ContextVar("receipt", default="outside")

    def job():
        received = receipt.get()
        receipt.set("job")
        return received

    async def exercise():
        token = receipt.set("collection")
        try:
            assert await process_memory.run_memory_heavy_job(job) == "collection"
            assert receipt.get() == "collection"
        finally:
            receipt.reset(token)

    asyncio.run(exercise())
