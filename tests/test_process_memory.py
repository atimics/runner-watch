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
