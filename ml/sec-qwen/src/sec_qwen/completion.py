from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sec_qwen.config import sha256_file


def _read_object(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def build_completion(
    *,
    dispatch_ref: str,
    executor_id: str,
    run_id: str,
    metrics_path: Path,
    artifact_path: Path,
    artifact_uri: str,
    provider_version: str,
    completed_at_ms: int,
    exit_code: int = 0,
    timed_out: bool = False,
) -> dict[str, Any]:
    if not dispatch_ref.startswith("artifact://sha256/") or len(dispatch_ref) != 82:
        raise ValueError("dispatch_ref must be an ilXyr SHA-256 artifact ref")
    if not executor_id.startswith("service://"):
        raise ValueError("executor_id must start with service://")
    if not run_id:
        raise ValueError("run_id must not be empty")
    if "://" not in artifact_uri or artifact_uri.startswith("file://"):
        raise ValueError("artifact_uri must be a non-file immutable provider handle")
    if not provider_version:
        raise ValueError("provider_version must not be empty")
    if completed_at_ms < 1:
        raise ValueError("completed_at_ms must be positive")
    metrics_document = _read_object(metrics_path, "metrics")
    metrics = metrics_document.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        raise ValueError("metrics document requires a non-empty metrics object")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) for value in metrics.values()
    ):
        raise ValueError("every completion metric must be numeric")
    return {
        "schema": "ilxyr.oci_job_completion.v1",
        "id": run_id,
        "dispatch_ref": dispatch_ref,
        "executor": {"id": executor_id, "kind": "service"},
        "exit_code": exit_code,
        "timed_out": timed_out,
        "metrics": metrics,
        "artifacts": [
            {
                "name": "adapter",
                "uri": artifact_uri,
                "sha256": sha256_file(artifact_path),
                "size_bytes": artifact_path.stat().st_size,
                "media_type": "application/x-tar",
                "provider_version": provider_version,
            }
        ],
        "completed_at_ms": completed_at_ms,
    }
