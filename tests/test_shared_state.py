from __future__ import annotations

from collections import defaultdict
from typing import Any

import pytest

from runner_web import shared_state


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.counters: dict[str, int] = defaultdict(int)

    def pipeline(self, *, transaction: bool) -> FakeRedis:
        assert transaction is True
        return self

    def __enter__(self) -> FakeRedis:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self) -> list[Any]:
        return []

    def setex(self, key: str, ttl: int, value: str) -> None:
        assert ttl > 0
        self.values[key] = value

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, *keys: str) -> int:
        for key in keys:
            self.values.pop(key, None)
        return len(keys)

    def lpush(self, key: str, value: str) -> None:
        self.lists[key].insert(0, value)

    def rpush(self, key: str, value: str) -> None:
        self.lists[key].append(value)

    def brpoplpush(self, source: str, destination: str, timeout: int) -> str | None:
        assert timeout > 0
        if not self.lists[source]:
            return None
        value = self.lists[source].pop()
        self.lists[destination].insert(0, value)
        return value

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        assert (start, end) == (0, -1)
        return list(self.lists[key])

    def lrem(self, key: str, count: int, value: str) -> int:
        assert count == 0
        before = len(self.lists[key])
        self.lists[key] = [item for item in self.lists[key] if item != value]
        return before - len(self.lists[key])

    def exists(self, key: str) -> int:
        return int(key in self.values)

    def eval(self, script: str, key_count: int, key: str, seconds: int) -> int:
        assert script and key_count == 1 and seconds > 0
        self.counters[key] += 1
        return self.counters[key]


def _configure(monkeypatch: Any) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(shared_state, "REDIS_URL", "redis://example")
    monkeypatch.setattr(shared_state, "REQUIRE_REDIS_TLS", False)
    monkeypatch.setattr(shared_state, "_CLIENT", fake)
    return fake


def test_cache_and_rate_limit_use_shared_redis(monkeypatch: Any) -> None:
    _configure(monkeypatch)

    shared_state.cache_set("radar", {"rows": [1]}, 60)

    assert shared_state.cache_get("radar") == {"rows": [1]}
    assert shared_state.rate_limit_allowed("visitor", 1, 60) is True
    assert shared_state.rate_limit_allowed("visitor", 1, 60) is False


def test_research_queue_contains_only_report_ids(monkeypatch: Any) -> None:
    _configure(monkeypatch)

    shared_state.enqueue_research_job("report-1")

    assert shared_state.dequeue_research_job(1) == "report-1"
    assert shared_state.recover_research_jobs() == 1
    assert shared_state.dequeue_research_job(1) == "report-1"
    shared_state.acknowledge_research_job("report-1")
    assert shared_state.dequeue_research_job(1) is None


def test_production_redis_requires_an_encrypted_connection(monkeypatch: Any) -> None:
    monkeypatch.setattr(shared_state, "REDIS_URL", "redis://example")
    monkeypatch.setattr(shared_state, "REQUIRE_REDIS_TLS", True)
    with pytest.raises(RuntimeError, match="must use TLS"):
        shared_state.redis_configured()

    monkeypatch.setattr(shared_state, "REDIS_URL", "rediss://example")
    assert shared_state.redis_configured() is True
