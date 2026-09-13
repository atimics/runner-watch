from __future__ import annotations

import asyncio

from tests.conftest import resume_running_loop, suspend_running_loop


def test_suspend_and_resume_round_trips_the_thread_loop() -> None:
    loop = asyncio.new_event_loop()
    asyncio.events._set_running_loop(loop)
    try:
        previous = suspend_running_loop()
        assert previous is loop
        assert asyncio.events._get_running_loop() is None
        resume_running_loop(previous)
        assert asyncio.events._get_running_loop() is loop
    finally:
        asyncio.events._set_running_loop(None)
        loop.close()


def test_suspend_is_a_no_op_without_a_running_loop() -> None:
    asyncio.events._set_running_loop(None)
    assert suspend_running_loop() is None
    assert asyncio.events._get_running_loop() is None


def test_asyncio_run_works_while_a_playwright_loop_is_suspended() -> None:
    stale = asyncio.new_event_loop()
    asyncio.events._set_running_loop(stale)
    try:
        previous = suspend_running_loop()
        assert asyncio.run(_value("ok")) == "ok"
    finally:
        resume_running_loop(previous)
        asyncio.events._set_running_loop(None)
        stale.close()


async def _value(value: str) -> str:
    await asyncio.sleep(0)
    return value