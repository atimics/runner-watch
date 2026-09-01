from __future__ import annotations

import hashlib
import io
import json
import urllib.request
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch, raises

from runner_web.sec_ilxyr import build_registry_update, publish_sec_corpus


class _Response(io.BytesIO):
    headers: dict[str, str] = {}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _write_corpus(directory: Path) -> dict[str, Any]:
    directory.mkdir()
    rows = [
        {"id": "one", "messages": []},
        {"id": "two", "messages": []},
    ]
    split = directory / "train.jsonl"
    split.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    summary = directory / "dataset-summary.json"
    summary.write_text(json.dumps({"examples": 2}) + "\n", encoding="utf-8")
    files = []
    for path in [split, summary]:
        files.append(
            {
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
                "media_type": (
                    "application/x-jsonlines" if path.suffix == ".jsonl" else "application/json"
                ),
            }
        )
    release = {
        "schema": "ilxyr.corpus_release.v1",
        "id": "dataset://stonks/feral-7b-sec/v2",
        "title": "Frozen SEC corpus",
        "version": "git-" + "a" * 12,
        "source": {
            "repository": "https://github.com/atimics/runner-watch",
            "revision": "a" * 40,
            "path": "exports/feral-7b-sec-v2",
        },
        "rights": {"license": "NOASSERTION"},
        "files": files,
        "metadata": {"examples": "2"},
    }
    (directory / "corpus-release.json").write_text(
        json.dumps(release, sort_keys=True) + "\n", encoding="utf-8"
    )
    return release


def _registry() -> dict[str, Any]:
    return {
        "schema": "ilxyr.research_registry.v1",
        "indexed_at_ms": 1,
        "stale_after_ms": 100,
        "heads": [],
        "projects": [
            {
                "project_id": "project://runner-watch/feral-7b-sec",
                "lifecycle_state": "blocked",
                "corpora": [
                    {
                        "corpus_id": "dataset://stonks/feral-7b-sec/v2",
                        "example_count": None,
                        "release_ref": None,
                        "materialization_ref": None,
                        "state": "blocked",
                        "evidence": "missing",
                    }
                ],
                "stages": [
                    {
                        "stage_id": "full_corpus_freeze",
                        "state": "blocked",
                        "evidence": "missing",
                        "refs": [],
                        "detail": "missing",
                    },
                    {
                        "stage_id": "corpus_materialization",
                        "state": "planned",
                        "evidence": "missing",
                        "refs": [],
                        "detail": "missing",
                    },
                ],
                "missing_requirements": [
                    {"requirement_id": "missing://qwen-sec/full-corpus-release"},
                    {"requirement_id": "missing://qwen-sec/materialization"},
                    {"requirement_id": "missing://qwen-sec/baselines"},
                ],
                "dispatches": [],
                "costs": {"spent": 0.0},
            }
        ],
    }


def _fake_service(
    monkeypatch: MonkeyPatch,
    release: dict[str, Any],
    *,
    materialization: dict[str, Any] | None = None,
) -> tuple[list[urllib.request.Request], str, str | None]:
    release_ref = "artifact://sha256/" + "b" * 64
    materialization_ref = "artifact://sha256/" + "c" * 64 if materialization else None
    responses = [
        {"artifact_ref": release_ref, "release": release},
    ]
    if materialization:
        responses.append({"artifact_ref": materialization_ref, "materialization": materialization})
    requests: list[urllib.request.Request] = []

    class _Opener:
        def open(self, request: urllib.request.Request, timeout: int) -> _Response:
            assert timeout == 12
            requests.append(request)
            return _Response(json.dumps(responses.pop(0)).encode())

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_handlers: _Opener())
    return requests, release_ref, materialization_ref


def test_publish_registers_verified_release_and_updates_only_corpus_state(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    release = _write_corpus(tmp_path / "corpus")
    requests, release_ref, _ = _fake_service(monkeypatch, release)
    result = publish_sec_corpus(
        tmp_path / "corpus",
        service_url="http://127.0.0.1:8787/",
        token="x" * 32,
        registry_template=_registry(),
        indexed_at_ms=123,
        timeout=12,
    )
    assert len(requests) == 1
    assert requests[0].full_url == "http://127.0.0.1:8787/v1/corpora"
    assert requests[0].headers["Authorization"] == "Bearer " + "x" * 32
    assert json.loads(requests[0].data) == release
    assert result.receipt["release_ref"] == release_ref
    assert result.receipt["materialization_ref"] is None
    assert result.receipt["training_authorized"] is False
    assert result.receipt["dispatch_ref"] is None
    assert result.registry is not None
    project = result.registry["projects"][0]
    corpus = project["corpora"][0]
    assert corpus["state"] == "registered"
    assert corpus["evidence"] == "recorded"
    assert corpus["example_count"] == 2
    assert project["lifecycle_state"] == "blocked"
    assert project["dispatches"] == []
    assert project["costs"]["spent"] == 0
    assert [item["requirement_id"] for item in project["missing_requirements"]] == [
        "missing://qwen-sec/materialization",
        "missing://qwen-sec/baselines",
    ]


def test_publish_records_matching_materialization_and_verified_registry_state(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    release = _write_corpus(tmp_path / "corpus")
    release_ref = "artifact://sha256/" + "b" * 64
    materialization = {
        "schema": "ilxyr.corpus_materialization.v1",
        "id": "materialization://stonks/feral-7b-sec/s3/v2",
        "corpus_ref": release_ref,
        "location": {
            "kind": "amazon_s3",
            "region": "us-west-2",
            "uri": "s3://research/feral-7b-sec/v2",
        },
        "objects": [
            {
                "path": item["path"],
                "uri": f"s3://research/feral-7b-sec/v2/{item['path']}",
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "provider_version": "version-1",
            }
            for item in release["files"]
        ],
        "verified_by": {"id": "service://runner-watch/materializer", "kind": "service"},
        "verified_at_ms": 123,
    }
    materialization_path = tmp_path / "materialization.json"
    materialization_path.write_text(json.dumps(materialization), encoding="utf-8")
    requests, _, materialization_ref = _fake_service(
        monkeypatch, release, materialization=materialization
    )
    result = publish_sec_corpus(
        tmp_path / "corpus",
        service_url="https://ilxyr.example",
        token="x" * 32,
        materialization_path=materialization_path,
        registry_template=_registry(),
        indexed_at_ms=123,
        timeout=12,
    )
    assert [request.full_url for request in requests] == [
        "https://ilxyr.example/v1/corpora",
        "https://ilxyr.example/v1/materializations",
    ]
    assert result.receipt["materialization_ref"] == materialization_ref
    assert result.registry is not None
    project = result.registry["projects"][0]
    assert project["corpora"][0]["state"] == "completed"
    assert project["corpora"][0]["evidence"] == "verified"
    stage = next(item for item in project["stages"] if item["stage_id"] == "corpus_materialization")
    assert stage["refs"] == [materialization_ref]
    assert [item["requirement_id"] for item in project["missing_requirements"]] == [
        "missing://qwen-sec/baselines"
    ]


def test_publish_rejects_tampered_corpus_before_network(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _write_corpus(tmp_path / "corpus")
    (tmp_path / "corpus" / "train.jsonl").write_text("tampered\n", encoding="utf-8")
    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_handlers: (_ for _ in ()).throw(AssertionError("network must not be used")),
    )
    with raises(ValueError, match="frozen size and hash"):
        publish_sec_corpus(
            tmp_path / "corpus",
            service_url="http://localhost:8787",
            token="x" * 32,
        )


def test_publish_rejects_unsafe_service_urls(tmp_path: Path) -> None:
    _write_corpus(tmp_path / "corpus")
    for url in [
        "http://ilxyr.example",
        "https://user:secret@ilxyr.example",
        "https://ilxyr.example/base",
    ]:
        with raises(ValueError, match="ilXyr corpus service"):
            publish_sec_corpus(tmp_path / "corpus", service_url=url, token="x" * 32)


def test_registry_update_requires_the_blocked_pilot() -> None:
    registry = _registry()
    registry["projects"][0]["lifecycle_state"] = "running"
    receipt = {
        "project_id": "project://runner-watch/feral-7b-sec",
        "corpus_id": "dataset://stonks/feral-7b-sec/v2",
        "release_ref": "artifact://sha256/" + "b" * 64,
        "materialization_ref": None,
        "example_count": 2,
        "source": {
            "repository": "https://github.com/atimics/runner-watch",
            "revision": "a" * 40,
        },
    }
    with raises(ValueError, match="must still be blocked"):
        build_registry_update(registry, receipt, indexed_at_ms=123)


def test_registry_update_never_downgrades_existing_materialization() -> None:
    registry = _registry()
    corpus = registry["projects"][0]["corpora"][0]
    corpus.update(
        {
            "release_ref": "artifact://sha256/" + "b" * 64,
            "materialization_ref": "artifact://sha256/" + "c" * 64,
            "state": "completed",
            "evidence": "verified",
        }
    )
    registry["projects"][0]["stages"][1].update(
        {
            "state": "completed",
            "evidence": "verified",
            "refs": ["artifact://sha256/" + "c" * 64],
        }
    )
    receipt = {
        "project_id": "project://runner-watch/feral-7b-sec",
        "corpus_id": "dataset://stonks/feral-7b-sec/v2",
        "release_ref": "artifact://sha256/" + "b" * 64,
        "materialization_ref": None,
        "example_count": 2,
        "source": {
            "repository": "https://github.com/atimics/runner-watch",
            "revision": "a" * 40,
        },
    }
    updated = build_registry_update(registry, receipt, indexed_at_ms=123)
    updated_corpus = updated["projects"][0]["corpora"][0]
    assert updated_corpus["state"] == "completed"
    assert updated_corpus["materialization_ref"] == "artifact://sha256/" + "c" * 64
