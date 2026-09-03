from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
from typing import Any

from sec_qwen.config import Config, sha256_file


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def chat_template_sha256(tokenizer: Any) -> str:
    template = tokenizer.chat_template
    if template is None:
        raise ValueError("tokenizer chat template must be present")
    if isinstance(template, str):
        encoded = template.encode()
    else:
        encoded = json.dumps(
            template,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    return hashlib.sha256(encoded).hexdigest()


def dependency_versions() -> dict[str, str]:
    return {
        name: importlib.metadata.version(name)
        for name in ("accelerate", "peft", "safetensors", "torch", "transformers")
    }


def implementation_receipt(
    config: Config,
    *,
    entrypoint: str,
    source_files: tuple[Path, ...],
) -> dict[str, Any]:
    package_root = Path(__file__).resolve().parents[2]
    lock_path = package_root / "uv.lock"
    return {
        "repository": "https://github.com/atimics/runner-watch",
        "revision": os.environ.get("RUNNER_WATCH_REVISION"),
        "entrypoint": entrypoint,
        "source_files": {
            str(path.resolve().relative_to(package_root)): sha256_file(path)
            for path in sorted(source_files)
        },
        "config_sha256": sha256_file(config.source_path),
        "dependency_lock_sha256": sha256_file(lock_path),
    }


def environment_receipt(torch: Any, model: Any) -> dict[str, Any]:
    if torch.cuda.is_available():
        accelerator = torch.cuda.get_device_name(0)
        accelerator_count = torch.cuda.device_count()
        runtime_version = torch.version.cuda or torch.version.hip
        device = str(model.device)
    elif torch.backends.mps.is_available():
        accelerator = "Apple Metal Performance Shaders"
        accelerator_count = 1
        runtime_version = platform.mac_ver()[0]
        device = str(model.device)
    else:
        accelerator = "cpu"
        accelerator_count = 1
        runtime_version = platform.platform()
        device = str(model.device)
    device_map = getattr(model, "hf_device_map", None)
    if device_map is None:
        resolved_device_map = device
    elif isinstance(device_map, dict):
        resolved_device_map = json.dumps(device_map, sort_keys=True, separators=(",", ":"))
    else:
        resolved_device_map = str(device_map)
    return {
        "executor_environment_ref": os.environ.get("EXECUTOR_ENVIRONMENT_REF"),
        "image": {
            "repository": os.environ.get("OCI_IMAGE_REPOSITORY"),
            "digest": os.environ.get("OCI_IMAGE_DIGEST"),
        },
        "hardware": {
            "accelerator": accelerator,
            "count": accelerator_count,
            "driver_version": os.environ.get("ACCELERATOR_DRIVER_VERSION"),
            "runtime_version": runtime_version,
        },
        "resolved_device_map": resolved_device_map,
    }


def data_receipt(
    config: Config,
    *,
    manifest: dict[str, Any],
    input_path: Path,
    tokenizer: Any,
) -> dict[str, Any]:
    return {
        "corpus_ref": manifest["id"],
        "release_id": manifest.get("release_id") or manifest.get("releaseId"),
        "manifest_sha256": sha256_file(config.dataset.corpus_manifest),
        "input_view": {
            "path": str(input_path.relative_to(config.dataset.corpus_manifest.parent)),
            "sha256": sha256_file(input_path),
        },
        "chat_template_sha256": chat_template_sha256(tokenizer),
        "max_sequence_length": config.training.max_seq_length,
    }


def software_receipt() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "dependencies": dependency_versions(),
    }
