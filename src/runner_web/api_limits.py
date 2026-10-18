"""Shared API admission and short-lived response replay."""

from __future__ import annotations

import base64
import json
import secrets
import time
import zlib
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException

from runner_web import shared_state

# One atomic admission covers the account, the whole service, and active requests.
ADMIT = """
local now = tonumber(ARGV[1])
local cap = tonumber(ARGV[2])
local concurrent = tonumber(ARGV[3])
local token = ARGV[4]
local jobs = tonumber(ARGV[5])
for i=1,5 do
  local age = 60
  if i == 3 or i == 4 then age = 120 end
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now - age)
end
if redis.call('ZCARD', KEYS[1]) >= cap or redis.call('ZCARD', KEYS[2]) >= 300
   or redis.call('ZCARD', KEYS[3]) >= concurrent or redis.call('ZCARD', KEYS[4]) >= 8
   or (jobs == 1 and redis.call('ZCARD', KEYS[5]) >= 2) then return 0 end
for i=1,4 do redis.call('ZADD', KEYS[i], now, token); redis.call('EXPIRE', KEYS[i], 180) end
if jobs == 1 then redis.call('ZADD', KEYS[5], now, token); redis.call('EXPIRE', KEYS[5], 180) end
return 1
"""


MAX_CACHE_BYTES = 32 * 1024 * 1024
CACHE_PUT = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[3], 'LIMIT', 0, 256)
local used = tonumber(redis.call('GET', KEYS[4]) or '0')
for _, id in ipairs(expired) do
  used = used - tonumber(redis.call('HGET', KEYS[3], id) or '0')
  redis.call('HDEL', KEYS[3], id)
  redis.call('ZREM', KEYS[2], id)
end
used = math.max(0, used)
redis.call('SET', KEYS[4], used)
local size = string.len(ARGV[2])
if used + size > tonumber(ARGV[4]) then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', 3600)
redis.call('HSET', KEYS[3], ARGV[1], size)
redis.call('ZADD', KEYS[2], tonumber(ARGV[3]) + 3600, ARGV[1])
redis.call('SET', KEYS[4], used + size)
for i=2,4 do redis.call('EXPIRE', KEYS[i], 7200) end
return 1
"""


def client() -> Any:
    try:
        return shared_state._client()
    except Exception as exc:
        raise HTTPException(
            503, "API limits are warming up. Retry shortly.", headers={"Retry-After": "10"}
        ) from exc


@contextmanager
def admission(user_id: str, *, paid: bool, jobs: bool = False):
    redis = client()
    token = secrets.token_hex(16)
    prefix = shared_state._key("api:" + user_id)
    keys = [
        prefix + ":rate",
        shared_state._key("api:global"),
        prefix + ":active",
        shared_state._key("api:global-active"),
        prefix + ":jobs",
    ]
    try:
        allowed = redis.eval(
            ADMIT, 5, *keys, time.time(), 60 if paid else 10, 5 if paid else 1, token, int(jobs)
        )
    except Exception as exc:
        raise HTTPException(
            503, "API limits are warming up. Retry shortly.", headers={"Retry-After": "10"}
        ) from exc
    if not allowed:
        raise HTTPException(
            429, "The request limit has been reached. Retry shortly.", headers={"Retry-After": "60"}
        )
    try:
        yield
    finally:
        try:
            redis.zrem(keys[2], token)
            redis.zrem(keys[3], token)
        except Exception:
            pass  # The lease expires even when Redis is briefly unavailable.


def save_result(identifier: str, result: dict[str, Any]) -> None:
    raw = json.dumps(result, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > 262144:
        raise HTTPException(413, "Use a smaller result page.")
    try:
        stored = client().eval(
            CACHE_PUT,
            4,
            shared_state._key("api:result:" + identifier),
            shared_state._key("api:result-expiry"),
            shared_state._key("api:result-sizes"),
            shared_state._key("api:result-bytes"),
            identifier,
            base64.b64encode(zlib.compress(raw)).decode(),
            time.time(),
            MAX_CACHE_BYTES,
        )
        if not stored:
            raise HTTPException(
                503, "The API result cache is full. Retry shortly.", headers={"Retry-After": "60"}
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "The result store is warming up. Retry shortly.") from exc


def load_result(identifier: str) -> dict[str, Any]:
    try:
        raw = client().get(shared_state._key("api:result:" + identifier))
        if raw:
            return json.loads(zlib.decompress(base64.b64decode(raw)))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, "The result store is warming up. Retry shortly.") from exc
    raise HTTPException(410, "This response has expired. Use a new Idempotency-Key for a new read.")
