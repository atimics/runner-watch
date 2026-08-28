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
        self.sets: dict[str, set[str]] = defaultdict(set)

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
        before = len(self.lists[key])
        if count == 0:
            self.lists[key] = [item for item in self.lists[key] if item != value]
        elif count > 0:
            removed = 0
            kept: list[str] = []
            for item in self.lists[key]:
                if item == value and removed < count:
                    removed += 1
                else:
                    kept.append(item)
            self.lists[key] = kept
        else:
            raise AssertionError("FakeRedis only supports non-negative LREM counts")
        return before - len(self.lists[key])

    def sadd(self, key: str, value: str) -> int:
        before = len(self.sets[key])
        self.sets[key].add(value)
        return len(self.sets[key]) - before

    def smembers(self, key: str) -> set[str]:
        return set(self.sets[key])

    def srem(self, key: str, value: str) -> int:
        existed = value in self.sets[key]
        self.sets[key].discard(value)
        return int(existed)

    def exists(self, key: str) -> int:
        return int(key in self.values)

    def eval(self, script: str, key_count: int, *args: Any) -> int:
        assert script
        if key_count == 1:
            key, seconds = args
            assert seconds > 0
            self.counters[key] += 1
            return self.counters[key]
        assert key_count == 4
        lease_key, processing_key, queue_key, workers_key, worker_token = args
        if self.exists(lease_key):
            return 0
        recovered = 0
        for report_id in list(self.lists[processing_key]):
            if self.lrem(processing_key, 1, report_id):
                self.rpush(queue_key, report_id)
                recovered += 1
        self.srem(workers_key, worker_token)
        return recovered


def _configure(monkeypatch: Any) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(shared_state, "REDIS_URL", "redis://example")
    monkeypatch.setattr(shared_state, "REQUIRE_REDIS_TLS", False)
    monkeypatch.setattr(shared_state, "_CLIENT", fake)
    monkeypatch.setattr(shared_state, "_BLOCKING_CLIENT", fake)
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

    assert shared_state.dequeue_research_job("worker-a", 1) == "report-1"
    assert shared_state.recover_research_jobs("worker-b") == 0
    shared_state.release_research_worker("worker-a")
    assert shared_state.recover_research_jobs("worker-b") == 1
    assert shared_state.dequeue_research_job("worker-b", 1) == "report-1"
    shared_state.acknowledge_research_job("worker-b", "report-1")
    assert shared_state.dequeue_research_job("worker-b", 1) is None


def test_web_redis_calls_do_not_use_the_blocking_queue_timeout(monkeypatch: Any) -> None:
    from redis import Redis

    clients: list[tuple[object, dict[str, Any]]] = []

    def fake_from_url(url: str, **options: Any) -> object:
        client = object()
        clients.append((client, options))
        assert url == "redis://example"
        return client

    monkeypatch.setattr(shared_state, "REDIS_URL", "redis://example")
    monkeypatch.setattr(shared_state, "REQUIRE_REDIS_TLS", False)
    monkeypatch.setattr(shared_state, "_CLIENT", None)
    monkeypatch.setattr(shared_state, "_BLOCKING_CLIENT", None)
    monkeypatch.setattr(Redis, "from_url", staticmethod(fake_from_url))

    fast_client = shared_state._client()
    blocking_client = shared_state._blocking_client()

    assert fast_client is clients[0][0]
    assert blocking_client is clients[1][0]
    assert clients[0][1]["socket_timeout"] == shared_state.REDIS_FAST_TIMEOUT_SECONDS
    assert clients[1][1]["socket_timeout"] == shared_state.REDIS_BLOCKING_TIMEOUT_SECONDS
    assert clients[0][1]["socket_timeout"] < clients[1][1]["socket_timeout"]


def test_production_redis_requires_an_encrypted_connection(monkeypatch: Any) -> None:
    monkeypatch.setattr(shared_state, "REDIS_URL", "redis://example")
    monkeypatch.setattr(shared_state, "REQUIRE_REDIS_TLS", True)
    with pytest.raises(RuntimeError, match="must use TLS"):
        shared_state.redis_configured()

    monkeypatch.setattr(shared_state, "REDIS_URL", "rediss://example")
    assert shared_state.redis_configured() is True
