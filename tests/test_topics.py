import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import init_db
from runner_web.topics import SQLiteTopicStore, TopicHub, TopicPolicy, TopicUpdate


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def test_topic_hub_caches_and_keeps_last_good_after_failure() -> None:
    clock = Clock(datetime(2026, 8, 25, 16, tzinfo=UTC))
    hub = TopicHub(clock=clock)
    policy = TopicPolicy(ttl_seconds=10, minimum_refresh_seconds=1, maximum_stale_seconds=60)
    calls = 0

    def success() -> TopicUpdate:
        nonlocal calls
        calls += 1
        return TopicUpdate(
            data={"price": 1.25}, source="test", as_of=clock(), collected_at=clock()
        )

    first = hub.get_or_refresh("market:quote:PEN", policy=policy, producer=success)
    second = hub.get_or_refresh("market:quote:PEN", policy=policy, producer=success)
    assert first.status == second.status == "fresh"
    assert calls == 1

    clock.advance(11)

    def failure() -> TopicUpdate:
        raise RuntimeError("provider unavailable")

    stale = hub.get_or_refresh("market:quote:PEN", policy=policy, producer=failure)
    assert stale.status == "stale"
    assert stale.data == {"price": 1.25}
    assert stale.error == "provider unavailable"


def test_topic_hub_coalesces_concurrent_refreshes() -> None:
    hub = TopicHub()
    policy = TopicPolicy(ttl_seconds=10, maximum_stale_seconds=60)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def producer() -> TopicUpdate:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return TopicUpdate(data=[1, 2, 3], source="test", as_of=datetime.now(UTC))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            hub.get_or_refresh,
            "market:bars:PEN:5m",
            policy=policy,
            producer=producer,
        )
        assert started.wait(timeout=2)
        second = pool.submit(
            hub.get_or_refresh,
            "market:bars:PEN:5m",
            policy=policy,
            producer=producer,
        )
        release.set()
        first_result = first.result(timeout=2)
        second_result = second.result(timeout=2)

    assert calls == 1
    assert first_result.data == second_result.data == [1, 2, 3]


def test_sqlite_topic_store_survives_a_new_hub(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "topics.db")
    init_db()
    policy = TopicPolicy(ttl_seconds=60, maximum_stale_seconds=120)
    first_hub = TopicHub(store=SQLiteTopicStore())
    first_hub.get_or_refresh(
        "market:quote:PEN",
        policy=policy,
        producer=lambda: TopicUpdate(
            data={"last": 1.5}, source="test", as_of=datetime.now(UTC)
        ),
    )

    restored = TopicHub(store=SQLiteTopicStore()).peek("market:quote:PEN", policy=policy)

    assert restored.data == {"last": 1.5}
    assert restored.source == "test"
    assert restored.status == "fresh"
