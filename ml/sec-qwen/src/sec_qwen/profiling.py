from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Any

from sec_qwen.config import Config, load_examples, sha256_file, validate_corpus


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def _split_profile(
    examples: list[dict[str, Any]], tokenizer: Any, max_seq_length: int
) -> dict[str, Any]:
    lengths: list[int] = []
    supervised_tokens = 0
    full_tokens = 0
    truncated_examples = 0
    unusable_examples = 0
    task_tokens: Counter[str] = Counter()
    for example in examples:
        messages = example["messages"]
        prompt = tokenizer.apply_chat_template(
            messages[:-1], tokenize=False, add_generation_prompt=True
        )
        complete = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=False
        )
        prompt_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
        input_ids = tokenizer(complete, add_special_tokens=False)["input_ids"]
        if input_ids[: len(prompt_ids)] != prompt_ids:
            raise ValueError("chat template prompt is not a prefix of the complete example")
        effective_length = min(len(input_ids), max_seq_length)
        supervised = max(0, effective_length - len(prompt_ids))
        lengths.append(effective_length)
        full_tokens += len(input_ids)
        supervised_tokens += supervised
        truncated_examples += int(len(input_ids) > max_seq_length)
        unusable_examples += int(supervised == 0)
        task_tokens[str(example.get("task") or "unknown")] += effective_length
    return {
        "examples": len(examples),
        "effective_tokens": sum(lengths),
        "full_tokens_before_truncation": full_tokens,
        "supervised_tokens": supervised_tokens,
        "truncated_examples": truncated_examples,
        "unusable_examples": unusable_examples,
        "length_tokens": {
            "maximum": max(lengths, default=0),
            "mean": round(sum(lengths) / len(lengths), 2) if lengths else 0.0,
            "p50": _percentile(lengths, 0.50),
            "p95": _percentile(lengths, 0.95),
        },
        "task_tokens": dict(sorted(task_tokens.items())),
    }


def profile_corpus(
    config: Config,
    *,
    tokenizer: Any | None = None,
    tokens_per_gpu_hour: float | None = None,
    gpu_hour_price: float | None = None,
    overhead_fraction: float = 0.25,
) -> dict[str, Any]:
    if (tokens_per_gpu_hour is None) != (gpu_hour_price is None):
        raise ValueError("token throughput and GPU price must be supplied together")
    if tokens_per_gpu_hour is not None and tokens_per_gpu_hour <= 0:
        raise ValueError("tokens_per_gpu_hour must be positive")
    if gpu_hour_price is not None and gpu_hour_price <= 0:
        raise ValueError("gpu_hour_price must be positive")
    if overhead_fraction < 0:
        raise ValueError("overhead_fraction must not be negative")

    manifest = validate_corpus(config)
    if tokenizer is None:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            config.model.model_id,
            revision=config.model.revision,
            trust_remote_code=False,
        )
    corpus_directory = config.dataset.corpus_manifest.parent
    split_names = [
        config.dataset.train_file,
        config.dataset.validation_file,
        *config.dataset.evaluation_files,
    ]
    splits = {
        name: _split_profile(
            load_examples(corpus_directory / name),
            tokenizer,
            config.training.max_seq_length,
        )
        for name in split_names
    }
    train_profile = splits[config.dataset.train_file]
    training_token_passes = round(int(train_profile["effective_tokens"]) * config.training.epochs)
    effective_batch_size = (
        config.training.per_device_batch_size * config.training.gradient_accumulation_steps
    )
    optimizer_steps = math.ceil(
        int(train_profile["examples"]) * config.training.epochs / effective_batch_size
    )
    result: dict[str, Any] = {
        "schema": "stonks.sec_qwen_profile.v1",
        "corpus": {
            "id": manifest["id"],
            "manifest_sha256": sha256_file(config.dataset.corpus_manifest),
        },
        "model": {"id": config.model.model_id, "revision": config.model.revision},
        "training": {
            "epochs": config.training.epochs,
            "effective_batch_size": effective_batch_size,
            "optimizer_steps": optimizer_steps,
            "estimated_training_token_passes": training_token_passes,
        },
        "splits": splits,
    }
    if tokens_per_gpu_hour is not None and gpu_hour_price is not None:
        gpu_hours = training_token_passes / tokens_per_gpu_hour
        base_cost = gpu_hours * gpu_hour_price
        result["budget"] = {
            "tokens_per_gpu_hour": tokens_per_gpu_hour,
            "gpu_hour_price": gpu_hour_price,
            "estimated_gpu_hours": round(gpu_hours, 4),
            "estimated_training_cost": round(base_cost, 2),
            "overhead_fraction": overhead_fraction,
            "recommended_cost_limit": round(base_cost * (1 + overhead_fraction), 2),
        }
    return result


def write_profile(path: Path, profile: dict[str, Any]) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
