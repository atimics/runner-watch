from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MAX_RESPONSE_BYTES = 1024 * 1024
PROJECT_ID = "project://runner-watch/feral-7b-sec"
RELEASE_REQUIREMENT = "missing://qwen-sec/full-corpus-release"
MATERIALIZATION_REQUIREMENT = "missing://qwen-sec/materialization"


@dataclass(frozen=True, slots=True)
class PublicationResult:
    receipt: dict[str, Any]
    registry: dict[str, Any] | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not readable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _service_origin(value: str) -> str:
    candidate = value.strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("ilXyr corpus service URL is invalid") from exc
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("ilXyr corpus service URL must be an origin without credentials")
    hostname = parsed.hostname.lower()
    try:
        is_loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        is_loopback = hostname == "localhost"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
        raise ValueError("ilXyr corpus service must use HTTPS or loopback HTTP")
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, "", "", ""))


def _artifact_ref(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.startswith("artifact://sha256/"):
        raise ValueError(f"{label} is not an ilXyr artifact reference")
    digest = value.removeprefix("artifact://sha256/")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{label} is not an ilXyr artifact reference")
    return value


def _post_json(
    origin: str,
    path: str,
    token: str,
    payload: dict[str, Any],
    *,
    timeout: int,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{origin}{path}",
        data=_canonical_json(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_RESPONSE_BYTES:
                raise ValueError("ilXyr corpus service response is too large")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"ilXyr corpus service rejected the request with HTTP {exc.code}") from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise ValueError("ilXyr corpus service could not be reached") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("ilXyr corpus service response is too large")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("ilXyr corpus service returned unreadable JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("ilXyr corpus service returned a non-object response")
    return value


def _validated_release(corpus_directory: Path) -> tuple[dict[str, Any], str, int]:
    directory = corpus_directory.resolve()
    manifest_path = directory / "corpus-release.json"
    release = _json_object(manifest_path, "corpus release")
    if release.get("schema") != "ilxyr.corpus_release.v1":
        raise ValueError("corpus release schema must be ilxyr.corpus_release.v1")
    if not str(release.get("id") or "").startswith("dataset://"):
        raise ValueError("SEC corpus release must use a dataset:// ID")
    source = release.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("revision"), str):
        raise ValueError("corpus release must include a source revision")
    revision = source["revision"]
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("corpus source revision must be a full lowercase Git commit SHA")
    files = release.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("corpus release must list files")
    paths: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("corpus release file entries must be objects")
        relative = item.get("path")
        expected_hash = item.get("sha256")
        expected_size = item.get("size_bytes")
        if not isinstance(relative, str) or not relative or relative.startswith("/"):
            raise ValueError("corpus release contains an unsafe file path")
        parts = Path(relative).parts
        if any(part in {"", ".", ".."} for part in parts) or relative in paths:
            raise ValueError("corpus release contains an unsafe or duplicate file path")
        paths.add(relative)
        path = directory.joinpath(*parts)
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory):
            raise ValueError(f"corpus file is missing or unsafe: {relative}")
        if path.stat().st_size != expected_size or _sha256(path) != expected_hash:
            raise ValueError(f"corpus file does not match its frozen size and hash: {relative}")
    metadata = release.get("metadata")
    examples = metadata.get("examples") if isinstance(metadata, dict) else None
    if not isinstance(examples, str) or not examples.isdecimal() or int(examples) < 1:
        raise ValueError("corpus release metadata.examples must be a positive integer string")
    summary = _json_object(directory / "dataset-summary.json", "dataset summary")
    if summary.get("examples") != int(examples):
        raise ValueError("dataset summary example count does not match the corpus release")
    return release, _sha256(manifest_path), int(examples)


def _validated_materialization(
    path: Path,
    *,
    release: dict[str, Any],
    release_ref: str,
) -> dict[str, Any]:
    materialization = _json_object(path, "corpus materialization")
    if materialization.get("schema") != "ilxyr.corpus_materialization.v1":
        raise ValueError("materialization schema must be ilxyr.corpus_materialization.v1")
    if materialization.get("corpus_ref") != release_ref:
        raise ValueError("materialization corpus_ref does not match the registered release")
    objects = materialization.get("objects")
    if not isinstance(objects, list):
        raise ValueError("materialization must list objects")
    expected = {item["path"]: (item["sha256"], item["size_bytes"]) for item in release["files"]}
    actual: dict[str, tuple[Any, Any]] = {}
    for item in objects:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("materialization object entries must name a path")
        object_path = item["path"]
        if object_path in actual:
            raise ValueError("materialization contains duplicate object paths")
        actual[object_path] = (item.get("sha256"), item.get("size_bytes"))
    if actual != expected:
        raise ValueError("materialization objects do not match the frozen corpus")
    return materialization


def build_registry_update(
    registry: dict[str, Any],
    receipt: dict[str, Any],
    *,
    indexed_at_ms: int,
) -> dict[str, Any]:
    updated = copy.deepcopy(registry)
    if updated.get("schema") != "ilxyr.research_registry.v1":
        raise ValueError("registry template schema must be ilxyr.research_registry.v1")
    projects = updated.get("projects")
    if not isinstance(projects, list):
        raise ValueError("registry template must contain projects")
    project = next(
        (
            item
            for item in projects
            if isinstance(item, dict) and item.get("project_id") == receipt["project_id"]
        ),
        None,
    )
    if project is None:
        raise ValueError(f"registry template does not contain {receipt['project_id']}")
    if project.get("lifecycle_state") != "blocked":
        raise ValueError("registry project must still be blocked before this corpus update")
    corpora = project.get("corpora")
    if not isinstance(corpora, list):
        raise ValueError("registry project must contain corpora")
    corpus = next(
        (
            item
            for item in corpora
            if isinstance(item, dict) and item.get("corpus_id") == receipt["corpus_id"]
        ),
        None,
    )
    if corpus is None:
        raise ValueError(f"registry project does not contain corpus {receipt['corpus_id']}")
    release_ref = receipt["release_ref"]
    existing_release_ref = corpus.get("release_ref")
    if existing_release_ref not in {None, release_ref}:
        raise ValueError("registry corpus already names a different release artifact")
    new_materialization_ref = receipt.get("materialization_ref")
    existing_materialization_ref = corpus.get("materialization_ref")
    if (
        existing_materialization_ref is not None
        and new_materialization_ref is not None
        and existing_materialization_ref != new_materialization_ref
    ):
        raise ValueError("registry corpus already names a different materialization artifact")
    materialization_ref = new_materialization_ref or existing_materialization_ref
    corpus.update(
        {
            "example_count": receipt["example_count"],
            "release_ref": release_ref,
            "materialization_ref": materialization_ref,
            "state": "completed" if materialization_ref else "registered",
            "evidence": "verified" if materialization_ref else "recorded",
        }
    )
    stages = project.get("stages")
    if not isinstance(stages, list):
        raise ValueError("registry project must contain stages")
    by_id = {item.get("stage_id"): item for item in stages if isinstance(item, dict)}
    freeze = by_id.get("full_corpus_freeze")
    materialization = by_id.get("corpus_materialization")
    if not isinstance(freeze, dict) or not isinstance(materialization, dict):
        raise ValueError("registry project is missing corpus lifecycle stages")
    freeze.update(
        {
            "state": "registered",
            "evidence": "recorded",
            "refs": [release_ref],
            "detail": "The frozen full corpus release is registered in the ilXyr corpus ledger.",
        }
    )
    if materialization_ref:
        materialization.update(
            {
                "state": "completed",
                "evidence": "verified",
                "refs": [materialization_ref],
                "detail": "ilXyr recorded a read-back verified cloud materialization receipt.",
            }
        )
    missing = project.get("missing_requirements")
    if not isinstance(missing, list):
        raise ValueError("registry project must contain missing requirements")
    resolved = {RELEASE_REQUIREMENT}
    if materialization_ref:
        resolved.add(MATERIALIZATION_REQUIREMENT)
    project["missing_requirements"] = [
        item
        for item in missing
        if not isinstance(item, dict) or item.get("requirement_id") not in resolved
    ]
    source = receipt["source"]
    source_url = f"{source['repository'].rstrip('/')}/tree/{source['revision']}"
    heads = updated.get("heads")
    if not isinstance(heads, list):
        raise ValueError("registry template must contain heads")
    matching_head = next(
        (item for item in heads if isinstance(item, dict) and item.get("source") == source_url),
        None,
    )
    head = {
        "source": source_url,
        "kind": "publication_index",
        "head": source["revision"],
        "indexed_at_ms": indexed_at_ms,
    }
    if matching_head is None:
        heads.append(head)
    else:
        matching_head.update(head)
    updated["indexed_at_ms"] = indexed_at_ms
    return updated


def publish_sec_corpus(
    corpus_directory: Path,
    *,
    service_url: str,
    token: str,
    materialization_path: Path | None = None,
    registry_template: dict[str, Any] | None = None,
    indexed_at_ms: int | None = None,
    timeout: int = 30,
) -> PublicationResult:
    origin = _service_origin(service_url)
    if len(token) < 32:
        raise ValueError("ILXYR_CORPUS_TOKEN must contain at least 32 characters")
    if timeout < 1 or timeout > 300:
        raise ValueError("timeout must be between 1 and 300 seconds")
    release, manifest_sha256, example_count = _validated_release(corpus_directory)
    registered = _post_json(origin, "/v1/corpora", token, release, timeout=timeout)
    release_ref = _artifact_ref(registered.get("artifact_ref"), "registered corpus artifact_ref")
    if registered.get("release") != release:
        raise ValueError("ilXyr corpus service returned different release content")
    materialization_ref = None
    if materialization_path is not None:
        materialization = _validated_materialization(
            materialization_path,
            release=release,
            release_ref=release_ref,
        )
        recorded = _post_json(
            origin,
            "/v1/materializations",
            token,
            materialization,
            timeout=timeout,
        )
        materialization_ref = _artifact_ref(
            recorded.get("artifact_ref"), "registered materialization artifact_ref"
        )
        if recorded.get("materialization") != materialization:
            raise ValueError("ilXyr corpus service returned different materialization content")
    receipt = {
        "schema": "stonks.sec_ilxyr_publication.v1",
        "project_id": PROJECT_ID,
        "corpus_id": release["id"],
        "corpus_manifest_sha256": manifest_sha256,
        "source": release["source"],
        "example_count": example_count,
        "release_ref": release_ref,
        "materialization_ref": materialization_ref,
        "training_authorized": False,
        "dispatch_ref": None,
    }
    registry = None
    if registry_template is not None:
        registry = build_registry_update(
            registry_template,
            receipt,
            indexed_at_ms=(int(time.time() * 1000) if indexed_at_ms is None else indexed_at_ms),
        )
    return PublicationResult(receipt=receipt, registry=registry)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical_json(value) + "\n", encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register a frozen SEC corpus with ilXyr and emit evidence-only registry data"
    )
    parser.add_argument("corpus_directory", type=Path)
    parser.add_argument("--service-url", default="http://127.0.0.1:8787")
    parser.add_argument("--materialization", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--registry-template", type=Path)
    parser.add_argument("--registry-output", type=Path)
    parser.add_argument("--timeout", type=int, default=30)
    arguments = parser.parse_args()
    if bool(arguments.registry_template) != bool(arguments.registry_output):
        parser.error("--registry-template and --registry-output must be used together")
    if (
        arguments.registry_template
        and arguments.registry_template.resolve() == arguments.registry_output.resolve()
    ):
        parser.error("--registry-output must be separate from --registry-template")
    token = os.getenv("ILXYR_CORPUS_TOKEN", "")
    if not token:
        parser.error("set ILXYR_CORPUS_TOKEN before publishing")
    registry_template = (
        _json_object(arguments.registry_template, "registry template")
        if arguments.registry_template
        else None
    )
    try:
        result = publish_sec_corpus(
            arguments.corpus_directory,
            service_url=arguments.service_url,
            token=token,
            materialization_path=arguments.materialization,
            registry_template=registry_template,
            timeout=arguments.timeout,
        )
    except ValueError as exc:
        parser.error(str(exc))
    _write_json(arguments.receipt, result.receipt)
    if arguments.registry_output and result.registry:
        _write_json(arguments.registry_output, result.registry)
    print(_canonical_json(result.receipt))


if __name__ == "__main__":
    main()
