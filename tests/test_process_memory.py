from __future__ import annotations

import asyncio
import logging

from runner_web import process_memory


def _job() -> str:
    return "done"


def test_a_job_that_grows_the_process_is_named(monkeypatch, caplog):
    readings = iter([200.0, 260.0])
    monkeypatch.setattr(process_memory, "rss_mb", lambda: next(readings))
    monkeypatch.setattr(process_memory, "peak_rss_mb", lambda: 300.0)

    with caplog.at_level(logging.WARNING, logger="runner_web.process_memory"):
        assert asyncio.run(process_memory.run_in_threadpool(_job)) == "done"

    assert "job=tests.test_process_memory._job" in caplog.text
    assert "grew_mb=60" in caplog.text


def test_small_growth_stays_quiet(monkeypatch, caplog):
    readings = iter([200.0, 205.0])
    monkeypatch.setattr(process_memory, "rss_mb", lambda: next(readings))

    with caplog.at_level(logging.WARNING, logger="runner_web.process_memory"):
        asyncio.run(process_memory.run_in_threadpool(_job))

    assert "memory_growth" not in caplog.text


def test_a_failing_job_is_still_measured_and_still_raises(monkeypatch, caplog):
    readings = iter([200.0, 400.0])
    monkeypatch.setattr(process_memory, "rss_mb", lambda: next(readings))

    def boom():
        raise ValueError("no")

    with caplog.at_level(logging.WARNING, logger="runner_web.process_memory"):
        try:
            asyncio.run(process_memory.run_in_threadpool(boom))
        except ValueError:
            pass
        else:
            raise AssertionError("the job's error was swallowed")

    assert "grew_mb=200" in caplog.text


def test_peak_memory_is_reported_in_megabytes():
    assert 1 < process_memory.peak_rss_mb() < 100_000


def test_the_memory_trend_is_logged_every_few_minutes(monkeypatch, caplog):
    monkeypatch.setattr(process_memory, "_last_trend_at", None)
    monkeypatch.setattr(process_memory, "rss_mb", lambda: 512.4)
    moments = iter([0.0, 60.0, 301.0])

    with caplog.at_level(logging.WARNING, logger="runner_web.process_memory"):
        for _ in range(3):
            process_memory.log_memory_trend(clock=lambda: next(moments))

    assert caplog.text.count("memory_trend rss_mb=512") == 2


def test_the_trend_names_heap_use_and_trims_freed_pages(monkeypatch, caplog):
    trimmed = []
    monkeypatch.setattr(process_memory, "_last_trend_at", None)
    monkeypatch.setattr(process_memory, "rss_mb", lambda: 600.0)
    monkeypatch.setattr(
        process_memory, "heap_mb", lambda: {"used": 120.0, "free": 380.0, "mmap": 40.0}
    )
    monkeypatch.setattr(process_memory, "trim_heap", lambda: trimmed.append(True))

    with caplog.at_level(logging.WARNING, logger="runner_web.process_memory"):
        process_memory.log_memory_trend(clock=lambda: 0.0)

    assert "heap_used_mb=120 heap_free_mb=380 heap_mmap_mb=40" in caplog.text
    assert "py_blocks=" in caplog.text and trimmed == [True]


def test_heap_figures_are_absent_without_glibc(monkeypatch):
    monkeypatch.setattr(process_memory, "_LIBC", None)

    assert process_memory.heap_mb() is None
    process_memory.trim_heap()  # nothing to trim, and no error
