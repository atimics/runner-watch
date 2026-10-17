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
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - 60)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now - 60)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now - 120)
if redis.call('ZCARD', KEYS[1]) >= cap or redis.call('ZCARD', KEYS[2]) >= 300
   or redis.call('ZCARD', KEYS[3]) >= concurrent then return 0 end
for i=1,3 do redis.call('ZADD', KEYS[i], now, token); redis.call('EXPIRE', KEYS[i], 180) end
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
        prefix + (":jobs" if jobs else ":rate"),
        shared_state._key("api:global"),
        prefix + ":active",
    ]
    try:
        allowed = redis.eval(
            ADMIT, 3, *keys, time.time(), 2 if jobs else 60 if paid else 10, 5 if paid else 1, token
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
        except Exception:
            pass  # The lease expires even when Redis is briefly unavailable.


def save_result(identifier: str, result: dict[str, Any]) -> None:
    raw = json.dumps(result, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > 262144:
        raise HTTPException(413, "Use a smaller result page.")
    try:
        client().set(
            shared_state._key("api:result:" + identifier),
            base64.b64encode(zlib.compress(raw)).decode(),
            ex=3600,
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
