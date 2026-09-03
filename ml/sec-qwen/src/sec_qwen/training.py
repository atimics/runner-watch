from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - Windows does not expose process rusage.
    resource = None  # type: ignore[assignment]

from sec_qwen.config import Config, load_examples, sha256_file, validate_corpus
from sec_qwen.receipts import (
    canonical_sha256,
    data_receipt,
    dependency_versions,
    environment_receipt,
    implementation_receipt,
    software_receipt,
)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _deterministic_tar(source: Path, destination: Path) -> None:
    with tarfile.open(destination, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            info = archive.gettarinfo(str(path), arcname=str(path.relative_to(source)))
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            with path.open("rb") as stream:
                archive.addfile(info, stream)


def _calibration_sample(
    examples: list[dict[str, Any]], sample_fraction: float
) -> list[dict[str, Any]]:
    if not 0 < sample_fraction <= 1:
        raise ValueError("sample_fraction must be greater than zero and at most one")
    if sample_fraction == 1:
        return examples
    selected = max(1, math.ceil(len(examples) * sample_fraction))
    return sorted(
        examples,
        key=lambda example: (
            hashlib.sha256(str(example["id"]).encode()).hexdigest(),
            str(example["id"]),
        ),
    )[:selected]


def _training_argument_values(
    config: Config,
    *,
    output: Path,
    has_validation: bool,
) -> dict[str, Any]:
    return {
        "output_dir": str(output / "checkpoints"),
        "num_train_epochs": config.training.epochs,
        "per_device_train_batch_size": config.training.per_device_batch_size,
        "per_device_eval_batch_size": config.training.evaluation_batch_size,
        "gradient_accumulation_steps": config.training.gradient_accumulation_steps,
        "learning_rate": config.training.learning_rate,
        "optim": config.training.optimizer,
        "lr_scheduler_type": config.training.lr_scheduler_type,
        "logging_steps": 1,
        "save_strategy": "epoch",
        "eval_strategy": "epoch" if has_validation else "no",
        "eval_on_start": config.training.eval_on_start and has_validation,
        "bf16": config.training.bf16,
        "fp16": False,
        "gradient_checkpointing": config.training.gradient_checkpointing,
        "dataloader_num_workers": config.training.dataloader_num_workers,
        "dataloader_persistent_workers": config.training.dataloader_num_workers > 0,
        "dataloader_in_order": config.training.dataloader_in_order,
        "report_to": "none",
        "seed": config.training.seed,
        "data_seed": config.training.seed,
        "remove_unused_columns": False,
    }


def train(
    config: Config,
    *,
    sample_fraction: float = 1.0,
    calibration: bool = False,
    output_directory: Path | None = None,
) -> dict[str, Any]:
    import torch
    from peft import LoraConfig, get_peft_model
    from torch.utils.data import Dataset
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        Trainer,
        TrainingArguments,
        set_seed,
    )

    manifest = validate_corpus(config)
    output = output_directory or config.output_directory
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    random.seed(config.training.seed)
    set_seed(config.training.seed)
    torch.use_deterministic_algorithms(True, warn_only=True)

    corpus_directory = config.dataset.corpus_manifest.parent
    all_train_examples = load_examples(corpus_directory / config.dataset.train_file)
    validation_examples = load_examples(corpus_directory / config.dataset.validation_file)
    if not all_train_examples:
        raise ValueError("training split must not be empty")
    train_examples = _calibration_sample(all_train_examples, sample_fraction)
    all_validation_examples = validation_examples
    if calibration:
        validation_examples = _calibration_sample(validation_examples, sample_fraction)
    tokenizer = AutoTokenizer.from_pretrained(
        config.model.model_id,
        revision=config.model.revision,
        trust_remote_code=False,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token

    class ChatDataset(Dataset):
        def __init__(self, examples: list[dict[str, Any]]) -> None:
            self.examples = examples

        def __len__(self) -> int:
            return len(self.examples)

        def __getitem__(self, index: int) -> dict[str, list[int]]:
            messages = self.examples[index]["messages"]
            prompt = tokenizer.apply_chat_template(
                messages[:-1], tokenize=False, add_generation_prompt=True
            )
            complete = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=False
            )
            prompt_ids = tokenizer(
                prompt,
                add_special_tokens=False,
                truncation=True,
                max_length=config.training.max_seq_length,
            )["input_ids"]
            input_ids = tokenizer(
                complete,
                add_special_tokens=False,
                truncation=True,
                max_length=config.training.max_seq_length,
            )["input_ids"]
            if input_ids[: len(prompt_ids)] != prompt_ids:
                raise ValueError("chat template prompt is not a prefix of the complete example")
            labels = [-100] * len(prompt_ids) + input_ids[len(prompt_ids) :]
            if not any(label != -100 for label in labels):
                raise ValueError("example was truncated before its assistant answer")
            return {
                "input_ids": input_ids,
                "attention_mask": [1] * len(input_ids),
                "labels": labels,
            }

    def collate(items: list[dict[str, list[int]]]) -> dict[str, Any]:
        longest = max(len(item["input_ids"]) for item in items)
        input_ids = []
        attention_mask = []
        labels = []
        for item in items:
            padding = longest - len(item["input_ids"])
            input_ids.append(item["input_ids"] + [tokenizer.pad_token_id] * padding)
            attention_mask.append(item["attention_mask"] + [0] * padding)
            labels.append(item["labels"] + [-100] * padding)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
        }

    model_kwargs: dict[str, Any] = {
        "revision": config.model.revision,
        "trust_remote_code": False,
        "dtype": config.model.torch_dtype,
    }
    if config.model.attn_implementation:
        model_kwargs["attn_implementation"] = config.model.attn_implementation
    model = AutoModelForCausalLM.from_pretrained(config.model.model_id, **model_kwargs)
    model.config.use_cache = False
    if config.training.gradient_checkpointing:
        model.enable_input_require_grads()
    model = get_peft_model(
        model,
        LoraConfig(
            task_type="CAUSAL_LM",
            r=config.training.lora_r,
            lora_alpha=config.training.lora_alpha,
            lora_dropout=config.training.lora_dropout,
            target_modules=list(config.training.target_modules),
            bias="none",
        ),
    )
    arguments = TrainingArguments(
        **_training_argument_values(
            config,
            output=output,
            has_validation=bool(validation_examples),
        )
    )
    trainer = Trainer(
        model=model,
        args=arguments,
        train_dataset=ChatDataset(train_examples),
        eval_dataset=ChatDataset(validation_examples) if validation_examples else None,
        data_collator=collate,
    )
    if calibration and torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    started_at = time.perf_counter()
    train_result = trainer.train()
    runtime_seconds = time.perf_counter() - started_at
    adapter_directory = output / ("private-calibration-adapter" if calibration else "adapter")
    model.save_pretrained(adapter_directory, safe_serialization=True)
    tokenizer.save_pretrained(adapter_directory)
    adapter_tar = output / ("private-calibration-adapter.tar" if calibration else "adapter.tar")
    _deterministic_tar(adapter_directory, adapter_tar)
    metrics = {
        key: float(value)
        for key, value in train_result.metrics.items()
        if isinstance(value, (int, float))
    }
    _write_json(output / "training-metrics.json", {"metrics": metrics})
    artifact = {
        "name": "private-calibration-adapter" if calibration else "adapter",
        "path": adapter_tar.name,
        "sha256": sha256_file(adapter_tar),
        "size_bytes": adapter_tar.stat().st_size,
        "media_type": "application/x-tar",
        "visibility": "private" if calibration else "release-candidate",
    }
    if calibration:
        calibration_view = {
            "train": train_examples,
            "validation": validation_examples,
        }
        calibration_view_sha256 = canonical_sha256(calibration_view)
        effective_tokens = 0
        for example in train_examples:
            token_ids = tokenizer.apply_chat_template(
                example["messages"], tokenize=True, add_generation_prompt=False
            )
            effective_tokens += min(len(token_ids), config.training.max_seq_length)
        token_passes = effective_tokens * config.training.epochs
        environment = environment_receipt(torch, model)
        measurement = {
            "device": environment["hardware"]["accelerator"],
            "runtime_seconds": round(runtime_seconds, 4),
            "effective_token_passes": round(token_passes),
            "tokens_per_device_hour": round(token_passes / runtime_seconds * 3600, 2),
        }
        if resource is not None:
            peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            if sys.platform != "darwin":
                peak_rss *= 1024
            measurement["process_peak_rss_bytes"] = peak_rss
        if torch.cuda.is_available():
            measurement.update(
                {
                    "device_total_memory_bytes": torch.cuda.get_device_properties(0).total_memory,
                    "peak_device_memory_allocated_bytes": torch.cuda.max_memory_allocated(),
                    "peak_device_memory_reserved_bytes": torch.cuda.max_memory_reserved(),
                }
            )
        report = {
            "schema": "stonks.sec_qwen_calibration.v1",
            "purpose": "calibration",
            "model": {"id": config.model.model_id, "revision": config.model.revision},
            "implementation": implementation_receipt(
                config,
                entrypoint="ml/sec-qwen/src/sec_qwen/training.py:train",
                source_files=(
                    Path(__file__),
                    Path(__file__).with_name("config.py"),
                    Path(__file__).with_name("receipts.py"),
                ),
            ),
            "software": software_receipt(),
            "environment": environment,
            "runtime": {
                "torch_dtype": config.model.torch_dtype,
                "attention_backend": config.model.attn_implementation,
                "mask_backend": config.model.mask_backend,
                "device_map": "accelerate_single_process",
                "use_cache": False,
                "cache_implementation": "disabled",
                "gradient_checkpointing": config.training.gradient_checkpointing,
                "compile": {"enabled": False, "backend": None},
            },
            "trainer": {
                "loop": "transformers.Trainer",
                "optimizer": config.training.optimizer,
                "scheduler": config.training.lr_scheduler_type,
                "epochs": config.training.epochs,
                "per_device_train_batch_size": config.training.per_device_batch_size,
                "per_device_eval_batch_size": config.training.evaluation_batch_size,
                "gradient_accumulation_steps": config.training.gradient_accumulation_steps,
                "learning_rate": config.training.learning_rate,
                "save_strategy": "epoch",
                "eval_strategy": "epoch" if validation_examples else "no",
                "eval_on_start": config.training.eval_on_start and bool(validation_examples),
            },
            "data": {
                **data_receipt(
                    config,
                    manifest=manifest,
                    input_path=corpus_directory / config.dataset.train_file,
                    tokenizer=tokenizer,
                ),
                "calibration_view": {
                    "ref": f"view://feral-7b/calibration/{calibration_view_sha256}",
                    "sha256": calibration_view_sha256,
                },
            },
            "sample": {
                "fraction": sample_fraction,
                "population_examples": len(all_train_examples),
                "selected_examples": len(train_examples),
                "validation_population_examples": len(all_validation_examples),
                "validation_selected_examples": len(validation_examples),
                "selection": "lowest-sha256-example-id-v1",
                "ids_sha256": hashlib.sha256(
                    "\n".join(str(example["id"]) for example in train_examples).encode()
                ).hexdigest(),
                "validation_ids_sha256": hashlib.sha256(
                    "\n".join(str(example["id"]) for example in validation_examples).encode()
                ).hexdigest(),
            },
            "adapter": {
                "method": "lora",
                "rank": config.training.lora_r,
                "alpha": config.training.lora_alpha,
                "dropout": config.training.lora_dropout,
                "bias": "none",
                "target_modules": list(config.training.target_modules),
            },
            "determinism": {
                "seed": config.training.seed,
                "data_seed": config.training.seed,
                "dataloader_workers": config.training.dataloader_num_workers,
                "dataloader_in_order": config.training.dataloader_in_order,
                "persistent_workers": config.training.dataloader_num_workers > 0,
                "deterministic_algorithms": True,
                "warn_only": True,
            },
            "outputs": [
                "private-calibration-adapter.tar",
                "run-manifest.json",
                "training-metrics.json",
                "calibration-profile.json",
            ],
            "measurement": measurement,
            "metrics": metrics,
            "artifact": artifact,
            "training_authorized": False,
            "execution_authorized": False,
        }
        _write_json(output / "calibration-profile.json", report)
        _write_json(output / "run-manifest.json", report)
        return report
    provenance = {
        "schema": "stonks.sec_qwen_run.v1",
        "model": {"id": config.model.model_id, "revision": config.model.revision},
        "corpus": {
            "id": manifest["id"],
            "manifest_sha256": sha256_file(config.dataset.corpus_manifest),
        },
        "config_sha256": sha256_file(config.source_path),
        "seed": config.training.seed,
        "examples": {"train": len(train_examples), "validation": len(validation_examples)},
        "dependencies": dependency_versions(),
        "artifact": artifact,
    }
    _write_json(output / "run-manifest.json", provenance)
    return provenance
