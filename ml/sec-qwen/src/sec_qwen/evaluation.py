from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sec_qwen.config import Config, load_examples


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


def evaluate_model(
    config: Config,
    *,
    adapter_directory: Path,
    split_file: str,
    predictions_path: Path,
) -> dict[str, float]:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    corpus_directory = config.dataset.corpus_manifest.parent
    examples = load_examples(corpus_directory / split_file)
    if not examples:
        raise ValueError(f"evaluation split is empty: {split_file}")
    tokenizer = AutoTokenizer.from_pretrained(
        config.model.model_id,
        revision=config.model.revision,
        trust_remote_code=False,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    model_kwargs: dict[str, Any] = {
        "revision": config.model.revision,
        "trust_remote_code": False,
        "torch_dtype": config.model.torch_dtype,
        "device_map": "auto",
    }
    if config.model.attn_implementation:
        model_kwargs["attn_implementation"] = config.model.attn_implementation
    base_model = AutoModelForCausalLM.from_pretrained(config.model.model_id, **model_kwargs)
    model = PeftModel.from_pretrained(base_model, adapter_directory, is_trainable=False)
    model.eval()
    rows = []
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
        with torch.inference_mode():
            generated = model.generate(
                **inputs,
                do_sample=False,
                max_new_tokens=config.training.max_new_tokens,
                pad_token_id=tokenizer.eos_token_id,
            )
        predictions = tokenizer.batch_decode(
            generated[:, inputs["input_ids"].shape[1] :],
            skip_special_tokens=True,
        )
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
    return score_predictions(rows)
