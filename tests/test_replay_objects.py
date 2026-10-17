from __future__ import annotations

import hashlib
from io import BytesIO

import pytest

from runner_web import replay_objects as objects


class FakeStore:
    def __init__(self):
        self.files = {}
        self.corrupt = False

    def put_object(self, **params):
        self.files[params["Key"]] = params["Body"]

    def get_object(self, **params):
        return {"Body": BytesIO(b"broken" if self.corrupt else self.files[params["Key"]])}


@pytest.fixture
def storage(monkeypatch):
    store = FakeStore()
    monkeypatch.setenv("MEMECOIN_REPLAY_BUCKET", "test-replays")
    monkeypatch.setattr(objects, "client", lambda: store)
    return store


def test_upload_reads_back_and_verifies_the_exact_bytes(storage):
    content = b"GIF89a example"
    digest = hashlib.sha256(content).hexdigest()
    key = objects.save_gif(content, digest)
    assert objects.read_gif(key, digest) == content
    storage.corrupt = True
    with pytest.raises(ValueError, match="hash check"):
        objects.save_gif(content, digest)


def test_mismatched_hash_and_object_path_are_rejected(storage):
    with pytest.raises(ValueError, match="hash check"):
        objects.save_gif(b"example", "a" * 64)
    with pytest.raises(ValueError, match="configuration mismatch"):
        objects.read_gif("other-object", "a" * 64)
    assert storage.files == {}


def test_large_download_is_bounded_and_closed(storage, monkeypatch):
    monkeypatch.setattr(objects, "MAX_GIF_BYTES", 4)
    body = BytesIO(b"123456789")
    monkeypatch.setattr(storage, "get_object", lambda **kwargs: {"Body": body})
    with pytest.raises(ValueError, match="hash check"):
        objects.read_gif(objects.object_key("a" * 64), "a" * 64)
    assert body.closed
