"""Private, content-addressed storage for replay GIFs."""

from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache

MAX_GIF_BYTES = 8 * 1024 * 1024


class ObjectStoreUnavailable(RuntimeError):
    pass


def bucket() -> str:
    return os.getenv("MEMECOIN_REPLAY_BUCKET", "").strip()


@lru_cache(maxsize=1)
def client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("AWS_ENDPOINT_URL_S3"),
        region_name=os.getenv("AWS_REGION", "auto"),
        config=Config(
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def object_key(digest: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Invalid replay GIF hash")
    return f"memecoin-replays/gif/{digest}.gif"


def read_gif(key: str, digest: str) -> bytes:
    from botocore.exceptions import BotoCoreError, ClientError

    if not bucket() or key != object_key(digest):
        raise ValueError("Replay object configuration mismatch")
    try:
        response = client().get_object(Bucket=bucket(), Key=key)
        body = response["Body"]
        try:
            content = body.read(MAX_GIF_BYTES + 1)
        finally:
            body.close()
    except (BotoCoreError, ClientError) as exc:
        raise ObjectStoreUnavailable("Replay file storage is temporarily unavailable") from exc
    if len(content) > MAX_GIF_BYTES or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Stored GIF hash check failed")
    return content


def save_gif(content: bytes, digest: str) -> str:
    """Read back the uploaded bytes before the caller releases its database copy."""
    if not bucket():
        raise ValueError("Replay object bucket is required")
    if not content or len(content) > MAX_GIF_BYTES:
        raise ValueError("Replay GIF size is invalid")
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Replay GIF hash check failed")
    key = object_key(digest)
    client().put_object(
        Bucket=bucket(),
        Key=key,
        Body=content,
        ContentType="image/gif",
        CacheControl="public, max-age=31536000, immutable",
        Metadata={"sha256": digest},
    )
    read_gif(key, digest)
    return key
