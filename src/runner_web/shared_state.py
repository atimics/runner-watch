from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from typing import Any

LOG = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "").strip()
REQUIRE_REDIS_TLS = os.getenv("REQUIRE_REDIS_TLS", "0") == "1"
KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "stonks").strip() or "stonks"
RESEARCH_WORKER_LEASE_SECONDS = max(
    60, int(os.getenv("RESEARCH_WORKER_LEASE_SECONDS", "300"))
)

_CLIENT_LOCK = threading.Lock()
_CLIENT: Any | None = None


def redis_configured() -> bool:
    if REDIS_URL and REQUIRE_REDIS_TLS and not REDIS_URL.startswith("rediss://"):
        raise RuntimeError("REDIS_URL must use TLS in this deployment")
    return bool(REDIS_URL)


def _client() -> Any:
    global _CLIENT
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL is not configured")
    if REQUIRE_REDIS_TLS and not REDIS_URL.startswith("rediss://"):
        raise RuntimeError("REDIS_URL must use TLS in this deployment")
    with _CLIENT_LOCK:
        if _CLIENT is None:
            from redis import Redis

            _CLIENT = Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,



                socket_timeout=10,
                health_check_interval=30,
            )
        return _CLIENT


def _key(name: str) -> str:
    return f"{KEY_PREFIX}:{name}"


def cache_get(name: str) -> Any | None:
    if not REDIS_URL:
        return None
    try:
        value = _client().get(_key(f"cache:{name}"))
        return json.loads(value) if value is not None else None
    except Exception:
        LOG.exception("Shared cache read failed")
        return None


def cache_set(name: str, value: Any, ttl_seconds: int) -> None:
    if not REDIS_URL:
        return
    try:
        _client().setex(
            _key(f"cache:{name}"),
            max(1, ttl_seconds),
            json.dumps(value, separators=(",", ":")),
        )
    except Exception:
        LOG.exception("Shared cache write failed")


def cache_delete(*names: str) -> None:
    if not REDIS_URL or not names:
        return
    try:
        _client().delete(*[_key(f"cache:{name}") for name in names])
    except Exception:
        LOG.exception("Shared cache delete failed")


_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


def rate_limit_allowed(name: str, limit: int, seconds: int) -> bool | None:


    if not REDIS_URL:
        return None
    try:
        count = _client().eval(
            _RATE_LIMIT_SCRIPT,
            1,
            _key(f"rate:{name}"),
            max(1, seconds),
        )
        return int(count) <= limit
    except Exception:
        LOG.exception("Shared rate limit failed")
        return None


def _queue_key() -> str:
    return _key("research:queue")


def _worker_token(worker_id: str) -> str:
    return hashlib.blake2s(worker_id.encode(), digest_size=12).hexdigest()


def _processing_key(worker_token: str) -> str:
    return _key(f"research:processing:{worker_token}")


def _research_workers_key() -> str:
    return _key("research:workers")


def _research_worker_lease_key(worker_token: str) -> str:
    return _key(f"research:worker-lease:{worker_token}")


def enqueue_research_job(report_id: str) -> None:


    _client().lpush(_queue_key(), report_id)


def touch_research_worker(worker_id: str) -> None:


    if not REDIS_URL:
        return
    client = _client()
    worker_token = _worker_token(worker_id)
    with client.pipeline(transaction=True) as pipeline:
        pipeline.sadd(_research_workers_key(), worker_token)
        pipeline.setex(
            _research_worker_lease_key(worker_token),
            RESEARCH_WORKER_LEASE_SECONDS,
            "1",
        )
        pipeline.execute()


def release_research_worker(worker_id: str) -> None:


    if not REDIS_URL:
        return
    _client().delete(_research_worker_lease_key(_worker_token(worker_id)))


_RECOVER_STALE_WORKER_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
local jobs = redis.call('LRANGE', KEYS[2], 0, -1)
local recovered = 0
for _, job in ipairs(jobs) do
  if redis.call('LREM', KEYS[2], 1, job) > 0 then
    redis.call('RPUSH', KEYS[3], job)
    recovered = recovered + 1
  end
end
redis.call('SREM', KEYS[4], ARGV[1])
return recovered
"""


def _recover_stale_research_workers(client: Any, current_token: str) -> int:
    recovered = 0
    for worker_token in client.smembers(_research_workers_key()):
        worker_token = str(worker_token)
        if worker_token == current_token:
            continue
        recovered += int(
            client.eval(
                _RECOVER_STALE_WORKER_SCRIPT,
                4,
                _research_worker_lease_key(worker_token),
                _processing_key(worker_token),
                _queue_key(),
                _research_workers_key(),
                worker_token,
            )
        )
    return recovered


def recover_research_jobs(worker_id: str) -> int:


    if not REDIS_URL:
        return 0
    client = _client()
    worker_token = _worker_token(worker_id)
    report_ids = client.lrange(_processing_key(worker_token), 0, -1)
    recovered = _recover_stale_research_workers(client, worker_token)
    for report_id in report_ids:
        with client.pipeline(transaction=True) as pipeline:
            pipeline.lrem(_processing_key(worker_token), 1, report_id)
            pipeline.rpush(_queue_key(), report_id)
            recovered += 1
            pipeline.execute()
    touch_research_worker(worker_id)
    return recovered


def dequeue_research_job(worker_id: str, timeout_seconds: int = 5) -> str | None:


    client = _client()
    worker_token = _worker_token(worker_id)
    _recover_stale_research_workers(client, worker_token)
    touch_research_worker(worker_id)
    report_id = client.brpoplpush(
        _queue_key(),
        _processing_key(worker_token),
        timeout=max(1, timeout_seconds),
    )
    return str(report_id) if report_id else None


def acknowledge_research_job(worker_id: str, report_id: str) -> None:
    client = _client()
    client.lrem(_processing_key(_worker_token(worker_id)), 1, report_id)
