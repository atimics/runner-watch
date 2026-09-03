from __future__ import annotations

import copy
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # pragma: no cover - Windows does not expose process rusage.
    resource = None  # type: ignore[assignment]

from sec_qwen.config import Config, load_examples, validate_corpus
from sec_qwen.receipts import (
    canonical_sha256,
    data_receipt,
    environment_receipt,
    implementation_receipt,
    software_receipt,
)
from sec_qwen.training import _calibration_sample


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def score_predictions(rows: list[dict[str, Any]]) -> dict[str, float]:
    if not rows:
        raise ValueError("prediction set must not be empty")
    valid = 0
    exact_examples = 0
    exact_fields = 0
    total_fields = 0
    for row in rows:
        target = json.loads(str(row["target"]))
        total_fields += len(target)
        try:
            prediction = json.loads(str(row["prediction"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(prediction, dict):
            continue
        valid += 1
        exact_examples += int(prediction == target)
        exact_fields += sum(prediction.get(field) == value for field, value in target.items())
    count = len(rows)
    return {
        "sec_example_exact_rate": exact_examples / count,
        "sec_field_exact_rate": exact_fields / total_fields if total_fields else 0.0,
        "sec_json_valid_rate": valid / count,
    }


def _peak_process_rss_bytes() -> int | None:
    if resource is None:
        return None
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform != "darwin":
        peak_rss *= 1024
    return peak_rss


def _profile_sample(
    examples: list[dict[str, Any]], sample_fraction: float
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = _calibration_sample(examples, sample_fraction)
    view_sha256 = canonical_sha256(selected)
    return selected, {
        "fraction": sample_fraction,
        "population_examples": len(examples),
        "selected_examples": len(selected),
        "selection": "lowest-sha256-example-id-v1",
        "ids_sha256": hashlib.sha256(
            "\n".join(str(example["id"]) for example in selected).encode()
        ).hexdigest(),
        "input_view_ref": f"view://feral-7b/base/{view_sha256}",
        "input_view_sha256": view_sha256,
    }


def _evaluate(
    config: Config,
    *,
    adapter_directory: Path | None,
    split_file: str,
    predictions_path: Path,
    sample_fraction: float = 1.0,
) -> tuple[dict[str, float], dict[str, Any]]:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    manifest = validate_corpus(config)
    corpus_directory = config.dataset.corpus_manifest.parent
    input_path = corpus_directory / split_file
    all_examples = load_examples(input_path)
    if not all_examples:
        raise ValueError(f"evaluation split is empty: {split_file}")
    examples, sample = _profile_sample(all_examples, sample_fraction)
    total_started_at = time.perf_counter()
    tokenizer_started_at = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(
        config.model.model_id,
        revision=config.model.revision,
        trust_remote_code=False,
    )
    tokenizer_load_seconds = time.perf_counter() - tokenizer_started_at
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    model_kwargs: dict[str, Any] = {
        "revision": config.model.revision,
        "trust_remote_code": False,
        "dtype": config.model.torch_dtype,
        "device_map": config.model.evaluation_device_map,
    }
    if config.model.attn_implementation:
        model_kwargs["attn_implementation"] = config.model.attn_implementation
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()
    model_started_at = time.perf_counter()
    base_model = AutoModelForCausalLM.from_pretrained(config.model.model_id, **model_kwargs)
    model_load_seconds = time.perf_counter() - model_started_at
    base_model.config.use_cache = True
    if adapter_directory is None:
        model = base_model
    else:
        from peft import PeftModel

        model = PeftModel.from_pretrained(base_model, adapter_directory, is_trainable=False)
    model.eval()
    generation_config = copy.deepcopy(model.generation_config)
    generation_config.max_new_tokens = config.training.max_new_tokens
    generation_config.do_sample = False
    generation_config.num_beams = 1
    generation_config.pad_token_id = tokenizer.eos_token_id
    generation_config.cache_implementation = "dynamic"
    generation_config.validate()
    generation_config_sha256 = canonical_sha256(generation_config.to_dict())
    rows = []
    input_tokens = 0
    generated_tokens = 0
    generation_seconds = 0.0
    batch_size = config.training.evaluation_batch_size
    for start in range(0, len(examples), batch_size):
        batch = examples[start : start + batch_size]
        prompts = [
            tokenizer.apply_chat_template(
                example["messages"][:-1],
                tokenize=False,
                add_generation_prompt=True,
            )
            for example in batch
        ]
        inputs = tokenizer(
            prompts,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=config.training.max_seq_length,
        ).to(model.device)
        input_tokens += int(inputs["attention_mask"].sum().item())
        generation_started_at = time.perf_counter()
        with torch.inference_mode():
            generated = model.generate(**inputs, generation_config=generation_config)
        generation_seconds += time.perf_counter() - generation_started_at
        new_tokens = generated[:, inputs["input_ids"].shape[1] :]
        generated_tokens += int(new_tokens.numel())
        predictions = tokenizer.batch_decode(new_tokens, skip_special_tokens=True)
        rows.extend(
            {
                "id": example["id"],
                "prediction": prediction.strip(),
                "target": example["messages"][-1]["content"],
            }
            for example, prediction in zip(batch, predictions, strict=True)
        )
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    with predictions_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            stream.write("\n")
    metrics = score_predictions(rows)
    environment = environment_receipt(torch, model)
    measurement: dict[str, Any] = {
        "examples": len(examples),
        "input_tokens": input_tokens,
        "generated_tokens": generated_tokens,
        "tokenizer_load_seconds": round(tokenizer_load_seconds, 4),
        "model_load_seconds": round(model_load_seconds, 4),
        "generation_seconds": round(generation_seconds, 4),
        "total_runtime_seconds": round(time.perf_counter() - total_started_at, 4),
    }
    peak_rss = _peak_process_rss_bytes()
    if peak_rss is not None:
        measurement["process_peak_rss_bytes"] = peak_rss
    if torch.cuda.is_available():
        measurement.update(
            {
                "device_total_memory_bytes": torch.cuda.get_device_properties(0).total_memory,
                "peak_device_memory_allocated_bytes": torch.cuda.max_memory_allocated(),
                "peak_device_memory_reserved_bytes": torch.cuda.max_memory_reserved(),
            }
        )
    profile = {
        "schema": "stonks.sec_qwen_base_profile.v1",
        "purpose": "base_profile",
        "model": {
            "id": config.model.model_id,
            "revision": config.model.revision,
            "tokenizer_revision": config.model.revision,
            "trust_remote_code": False,
            "weight_ref": (
                f"weight://huggingface/{config.model.model_id}@{config.model.revision}"
            ),
        },
        "implementation": implementation_receipt(
            config,
            entrypoint="ml/sec-qwen/src/sec_qwen/evaluation.py:profile_base_model",
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
            "configured_device_map": config.model.evaluation_device_map,
            "resolved_device_map": environment["resolved_device_map"],
            "use_cache": True,
            "cache_implementation": "dynamic",
            "gradient_checkpointing": False,
            "compile": {"enabled": False, "backend": None},
        },
        "generation": {
            "resolved_config_sha256": generation_config_sha256,
            "max_new_tokens": config.training.max_new_tokens,
            "do_sample": False,
            "num_beams": 1,
            "batch_size": batch_size,
        },
        "data": data_receipt(
            config,
            manifest=manifest,
            input_path=input_path,
            tokenizer=tokenizer,
        ),
        "sample": sample,
        "adapter": (
            {"method": "none"}
            if adapter_directory is None
            else {"method": "lora", "path": str(adapter_directory)}
        ),
        "determinism": {
            "seed": config.training.seed,
            "data_seed": config.training.seed,
            "dataloader_workers": 0,
            "dataloader_in_order": True,
            "persistent_workers": False,
            "deterministic_algorithms": False,
            "warn_only": False,
        },
        "outputs": [
            "private-base-predictions.jsonl",
            "base-metrics.json",
            "base-profile.json",
        ],
        "measurement": measurement,
        "metrics": metrics,
        "execution_authorized": False,
    }
    return metrics, profile


def evaluate_model(
    config: Config,
    *,
    adapter_directory: Path | None,
    split_file: str,
    predictions_path: Path,
) -> dict[str, float]:
    metrics, _profile = _evaluate(
        config,
        adapter_directory=adapter_directory,
        split_file=split_file,
        predictions_path=predictions_path,
    )
    return metrics


def profile_base_model(
    config: Config,
    *,
    split_file: str,
    output_directory: Path,
    sample_fraction: float = 0.01,
) -> dict[str, Any]:
    if output_directory.exists() and any(output_directory.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output_directory}")
    output_directory.mkdir(parents=True, exist_ok=True)
    metrics, profile = _evaluate(
        config,
        adapter_directory=None,
        split_file=split_file,
        predictions_path=output_directory / "private-base-predictions.jsonl",
        sample_fraction=sample_fraction,
    )
    _write_json(output_directory / "base-metrics.json", {"metrics": metrics})
    _write_json(output_directory / "base-profile.json", profile)
    return profile
