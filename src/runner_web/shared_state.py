from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

LOG = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "").strip()
KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "stonks").strip() or "stonks"

_CLIENT_LOCK = threading.Lock()
_CLIENT: Any | None = None


def redis_configured() -> bool:
    return bool(REDIS_URL)


def _client() -> Any:
    global _CLIENT
    if not REDIS_URL:
        raise RuntimeError("REDIS_URL is not configured")
    with _CLIENT_LOCK:
        if _CLIENT is None:
            from redis import Redis

            _CLIENT = Redis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=2,
                # Blocking queue reads wait up to five seconds. Leave enough
                # headroom for Redis to send its empty response without the
                # client treating a normal queue poll as a network failure.
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
    """Return None when Redis is absent or unavailable so callers can use a local fallback."""

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


def _processing_key() -> str:
    return _key("research:processing")


def enqueue_research_job(report_id: str) -> None:
    """Queue only the public report ID. Provider credentials stay on the server."""

    _client().lpush(_queue_key(), report_id)


def recover_research_jobs() -> int:
    """Put jobs claimed by a stopped worker back on the durable queue."""

    if not REDIS_URL:
        return 0
    client = _client()
    report_ids = client.lrange(_processing_key(), 0, -1)
    recovered = 0
    for report_id in report_ids:
        with client.pipeline(transaction=True) as pipeline:
            pipeline.lrem(_processing_key(), 0, report_id)
            pipeline.rpush(_queue_key(), report_id)
            recovered += 1
            pipeline.execute()
    return recovered


def dequeue_research_job(timeout_seconds: int = 5) -> str | None:
    """Atomically claim one job while leaving it recoverable until it is acknowledged."""

    client = _client()
    report_id = client.brpoplpush(
        _queue_key(),
        _processing_key(),
        timeout=max(1, timeout_seconds),
    )
    return str(report_id) if report_id else None


def acknowledge_research_job(report_id: str) -> None:
    client = _client()
    client.lrem(_processing_key(), 0, report_id)
