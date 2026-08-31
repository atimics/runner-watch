from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelConfig:
    model_id: str
    revision: str
    torch_dtype: str
    attn_implementation: str | None


@dataclass(frozen=True)
class DatasetConfig:
    provider: str
    corpus_manifest: Path
    release_id: str | None
    manifest_sha256: str | None
    train_file: str
    validation_file: str
    evaluation_files: tuple[str, ...]


@dataclass(frozen=True)
class TrainingConfig:
    seed: int
    max_seq_length: int
    epochs: float
    per_device_batch_size: int
    gradient_accumulation_steps: int
    learning_rate: float
    lora_r: int
    lora_alpha: int
    lora_dropout: float
    target_modules: tuple[str, ...]
    gradient_checkpointing: bool
    bf16: bool
    max_new_tokens: int
    dataloader_num_workers: int
    evaluation_batch_size: int


@dataclass(frozen=True)
class Config:
    schema: str
    model: ModelConfig
    dataset: DatasetConfig
    training: TrainingConfig
    output_directory: Path
    source_path: Path


def _only_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    extras = sorted(set(value) - allowed)
    if extras:
        raise ValueError(f"{label} contains unknown keys: {', '.join(extras)}")


def _full_revision(value: str) -> bool:
    return len(value) == 40 and all(character in "0123456789abcdef" for character in value)


def load_config(path: Path) -> Config:
    source_path = path.resolve()
    with source_path.open("rb") as stream:
        raw = tomllib.load(stream)
    _only_keys(raw, {"schema", "model", "dataset", "training", "output"}, "config")
    if raw.get("schema") != "stonks.sec_qwen_training.v1":
        raise ValueError("schema must be stonks.sec_qwen_training.v1")
    model = dict(raw.get("model") or {})
    dataset = dict(raw.get("dataset") or {})
    training = dict(raw.get("training") or {})
    output = dict(raw.get("output") or {})
    _only_keys(
        model,
        {"model_id", "revision", "torch_dtype", "attn_implementation"},
        "model",
    )
    _only_keys(
        dataset,
        {
            "provider",
            "corpus_manifest",
            "release_id",
            "manifest_sha256",
            "train_file",
            "validation_file",
            "evaluation_files",
        },
        "dataset",
    )
    _only_keys(
        training,
        {
            "seed",
            "max_seq_length",
            "epochs",
            "per_device_batch_size",
            "gradient_accumulation_steps",
            "learning_rate",
            "lora_r",
            "lora_alpha",
            "lora_dropout",
            "target_modules",
            "gradient_checkpointing",
            "bf16",
            "max_new_tokens",
            "dataloader_num_workers",
            "evaluation_batch_size",
        },
        "training",
    )
    _only_keys(output, {"directory"}, "output")

    model_id = str(model.get("model_id") or "")
    revision = str(model.get("revision") or "")
    if not model_id or any(character.isspace() for character in model_id):
        raise ValueError("model.model_id must not be empty or contain whitespace")
    if not _full_revision(revision):
        raise ValueError("model.revision must be a full lowercase Git commit SHA")
    base = source_path.parent
    corpus_manifest = (base / str(dataset.get("corpus_manifest") or "")).resolve()
    dataset_provider = str(dataset.get("provider") or "ilxyr")
    if dataset_provider not in {"ilxyr", "braid"}:
        raise ValueError("dataset.provider must be ilxyr or braid")
    release_id = str(dataset.get("release_id") or "") or None
    manifest_sha256 = str(dataset.get("manifest_sha256") or "") or None
    if dataset_provider == "braid":
        if not release_id:
            raise ValueError("dataset.release_id is required for a Braid corpus")
        if manifest_sha256 is None or not _sha256_digest(manifest_sha256):
            raise ValueError(
                "dataset.manifest_sha256 must be a lowercase SHA-256 digest for a Braid corpus"
            )
    output_directory = (base / str(output.get("directory") or "")).resolve()
    evaluation_files = tuple(str(value) for value in dataset.get("evaluation_files") or ())
    target_modules = tuple(str(value) for value in training.get("target_modules") or ())
    if not target_modules:
        raise ValueError("training.target_modules must not be empty")
    config = Config(
        schema=str(raw["schema"]),
        model=ModelConfig(
            model_id=model_id,
            revision=revision,
            torch_dtype=str(model.get("torch_dtype") or "bfloat16"),
            attn_implementation=(
                str(model["attn_implementation"])
                if model.get("attn_implementation")
                else None
            ),
        ),
        dataset=DatasetConfig(
            provider=dataset_provider,
            corpus_manifest=corpus_manifest,
            release_id=release_id,
            manifest_sha256=manifest_sha256,
            train_file=str(dataset.get("train_file") or "train.jsonl"),
            validation_file=str(dataset.get("validation_file") or "validation.jsonl"),
            evaluation_files=evaluation_files,
        ),
        training=TrainingConfig(
            seed=int(training.get("seed", 17)),
            max_seq_length=int(training.get("max_seq_length", 8192)),
            epochs=float(training.get("epochs", 2.0)),
            per_device_batch_size=int(training.get("per_device_batch_size", 1)),
            gradient_accumulation_steps=int(training.get("gradient_accumulation_steps", 16)),
            learning_rate=float(training.get("learning_rate", 2e-4)),
            lora_r=int(training.get("lora_r", 32)),
            lora_alpha=int(training.get("lora_alpha", 64)),
            lora_dropout=float(training.get("lora_dropout", 0.05)),
            target_modules=target_modules,
            gradient_checkpointing=bool(training.get("gradient_checkpointing", True)),
            bf16=bool(training.get("bf16", True)),
            max_new_tokens=int(training.get("max_new_tokens", 512)),
            dataloader_num_workers=int(training.get("dataloader_num_workers", 0)),
            evaluation_batch_size=int(training.get("evaluation_batch_size", 1)),
        ),
        output_directory=output_directory,
        source_path=source_path,
    )
    if config.training.seed < 0 or config.training.max_seq_length < 256:
        raise ValueError("training seed and max_seq_length are invalid")
    if config.training.epochs <= 0 or config.training.learning_rate <= 0:
        raise ValueError("training epochs and learning_rate must be positive")
    if not 0 <= config.training.lora_dropout < 1:
        raise ValueError("training.lora_dropout must be between 0 and 1")
    if config.training.dataloader_num_workers < 0:
        raise ValueError("training.dataloader_num_workers must not be negative")
    if config.training.evaluation_batch_size < 1:
        raise ValueError("training.evaluation_batch_size must be positive")
    return config


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def validate_corpus(config: Config) -> dict[str, Any]:
    manifest = json.loads(config.dataset.corpus_manifest.read_text(encoding="utf-8"))
    actual_manifest_sha256 = sha256_file(config.dataset.corpus_manifest)
    if (
        config.dataset.manifest_sha256 is not None
        and actual_manifest_sha256 != config.dataset.manifest_sha256
    ):
        raise ValueError("corpus manifest hash does not match config")
    if config.dataset.provider == "braid":
        manifest = _validate_braid_manifest(config, manifest)
        declared = {str(item["path"]): item for item in manifest.get("artifacts") or []}
    else:
        if manifest.get("schema") != "ilxyr.corpus_release.v1":
            raise ValueError("corpus manifest schema must be ilxyr.corpus_release.v1")
        declared = {str(item["path"]): item for item in manifest.get("files") or []}
    corpus_directory = config.dataset.corpus_manifest.parent
    required = {
        config.dataset.train_file,
        config.dataset.validation_file,
        *config.dataset.evaluation_files,
    }
    missing = sorted(required - set(declared))
    if missing:
        raise ValueError(f"corpus manifest does not declare: {', '.join(missing)}")
    for name, item in declared.items():
        path = (corpus_directory / name).resolve()
        try:
            path.relative_to(corpus_directory.resolve())
        except ValueError as error:
            raise ValueError(f"corpus file escapes the release directory: {name}") from error
        if not path.is_file():
            raise ValueError(f"corpus file is missing: {name}")
        if sha256_file(path) != item.get("sha256"):
            raise ValueError(f"corpus file hash does not match manifest: {name}")
        declared_size = (
            item.get("bytes")
            if config.dataset.provider == "braid"
            else item.get("size_bytes")
        )
        if path.stat().st_size != declared_size:
            raise ValueError(f"corpus file size does not match manifest: {name}")
    return manifest


def _validate_braid_manifest(config: Config, manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schemaVersion") != "braid.release/v2":
        raise ValueError("Braid corpus manifest schema must be braid.release/v2")
    if manifest.get("status") != "RELEASED":
        raise ValueError("Braid corpus must have RELEASED status")
    release_id = str(manifest.get("releaseId") or "")
    if release_id != config.dataset.release_id:
        raise ValueError("Braid release ID does not match config")
    release_digest = str((manifest.get("digests") or {}).get("release") or "")
    if not _sha256_digest(release_digest) or not release_id.endswith(f"-{release_digest}"):
        raise ValueError("Braid release ID is not bound to its release digest")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("Braid corpus manifest does not declare artifacts")
    paths: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise ValueError("Braid corpus manifest contains an invalid artifact")
        if not isinstance(item.get("path"), str) or not _sha256_digest(
            str(item.get("sha256") or "")
        ):
            raise ValueError("Braid corpus manifest contains an invalid artifact binding")
        if not isinstance(item.get("bytes"), int) or int(item["bytes"]) < 0:
            raise ValueError("Braid corpus manifest contains an invalid artifact size")
        artifact_path = str(item["path"])
        if artifact_path in paths:
            raise ValueError("Braid corpus manifest contains a duplicate artifact path")
        paths.add(artifact_path)
    return {**manifest, "id": release_id}


def load_examples(path: Path) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            example = _unwrap_example(row, path, line_number)
            if example.get("schema") not in {
                "stonks.sec_chat_example.v1",
                "stonks.sec_chat_example.v2",
            }:
                raise ValueError(f"{path}:{line_number} has an unsupported schema")
            messages = example.get("messages")
            if not isinstance(messages, list) or [item.get("role") for item in messages] != [
                "system",
                "user",
                "assistant",
            ]:
                raise ValueError(
                    f"{path}:{line_number} must contain system/user/assistant messages"
                )
            json.loads(str(messages[-1].get("content") or ""))
            examples.append(example)
    return examples


def _unwrap_example(row: Any, path: Path, line_number: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"{path}:{line_number} must contain a JSON object")
    if row.get("schema") in {
        "stonks.sec_chat_example.v1",
        "stonks.sec_chat_example.v2",
    }:
        return row
    metadata = row.get("metadata")
    if (
        isinstance(metadata, dict)
        and isinstance(row.get("provenance"), dict)
        and isinstance(row.get("contentHash"), str)
        and metadata.get("schema")
        in {"stonks.sec_chat_example.v1", "stonks.sec_chat_example.v2"}
    ):
        example = dict(metadata)
        example.setdefault("id", row.get("id"))
        return example
    return row
