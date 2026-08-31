from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sec_qwen.benchmarks import confident_hallucination_rate, finqa_accuracy
from sec_qwen.config import Config, load_examples, sha256_file, validate_corpus


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_suite(
    output: Path,
    *,
    task: str,
    rows: list[dict[str, Any]],
    source: dict[str, Any],
) -> dict[str, Any]:
    if not rows:
        raise ValueError(f"{task} benchmark must not be empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(_canonical_json(row) + "\n")
    manifest = {
        "schema": "stonks.sec_qwen_benchmark_manifest.v1",
        "task": task,
        "source": source,
        "examples": len(rows),
        "file": {
            "path": output.name,
            "sha256": sha256_file(output),
            "size_bytes": output.stat().st_size,
        },
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(_canonical_json(manifest) + "\n", encoding="utf-8", newline="\n")
    return {
        "manifest": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        **manifest,
    }


def prepare_finqa(
    source_path: Path,
    output: Path,
    *,
    source_revision: str,
    limit: int | None = None,
) -> dict[str, Any]:
    if len(source_revision) != 40 or any(c not in "0123456789abcdef" for c in source_revision):
        raise ValueError("FinQA source revision must be a full lowercase Git commit SHA")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(source, list):
        raise ValueError("FinQA source must be a JSON array")
    selected = source[:limit] if limit is not None else source
    rows = []
    for item in selected:
        qa = item.get("qa") if isinstance(item, dict) else None
        if not isinstance(qa, dict) or "answer" not in qa:
            continue
        evidence = {
            "question": qa.get("question"),
            "retrieved_evidence": qa.get("model_input"),
        }
        rows.append(
            {
                "schema": "stonks.sec_qwen_benchmark.v1",
                "task": "finqa",
                "id": str(item["id"]),
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Answer the financial reasoning question using only the supplied "
                            "evidence. Return one JSON object with exactly one field: answer."
                        ),
                    },
                    {"role": "user", "content": _canonical_json(evidence)},
                ],
                "answer": str(qa["answer"]),
            }
        )
    return _write_suite(
        output,
        task="finqa",
        rows=rows,
        source={
            "repository": "https://github.com/czyssrs/FinQA",
            "revision": source_revision,
            "path": str(source_path.name),
            "sha256": sha256_file(source_path),
            "context_policy": "finqa-model-input-v1",
        },
    )


def prepare_citation_support(config: Config, output: Path) -> dict[str, Any]:
    corpus = validate_corpus(config)
    corpus_directory = config.dataset.corpus_manifest.parent
    examples = [
        example
        for filename in config.dataset.evaluation_files
        for example in load_examples(corpus_directory / filename)
        if example.get("task") == "insufficient_evidence"
    ]
    rows = []
    for example in examples:
        target = json.loads(str(example["messages"][-1]["content"]))
        messages = [dict(message) for message in example["messages"][:-1]]
        messages[0]["content"] += (
            " Include a numeric confidence from 0 to 1. Return exactly the fields concept, "
            "status, value, and confidence."
        )
        rows.append(
            {
                "schema": "stonks.sec_qwen_benchmark.v1",
                "task": "citation_support",
                "id": str(example["id"]),
                "messages": messages,
                "expected": {
                    "concept": target.get("concept"),
                    "status": "insufficient_evidence",
                    "value": None,
                },
            }
        )
    return _write_suite(
        output,
        task="citation_support",
        rows=rows,
        source={
            "corpus_id": corpus["id"],
            "corpus_manifest_sha256": sha256_file(config.dataset.corpus_manifest),
            "splits": list(config.dataset.evaluation_files),
        },
    )


def _read_suite(path: Path, task: str) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        rows = [json.loads(line) for line in stream if line.strip()]
    if not rows or any(
        row.get("schema") != "stonks.sec_qwen_benchmark.v1" or row.get("task") != task
        for row in rows
    ):
        raise ValueError(f"benchmark does not contain only {task} rows")
    ids = [str(row.get("id") or "") for row in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("benchmark IDs must be non-empty and unique")
    return rows


def evaluate_benchmark(
    config: Config,
    *,
    dataset_path: Path,
    task: str,
    predictions_path: Path,
    adapter_directory: Path | None = None,
) -> dict[str, float]:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if task not in {"finqa", "citation_support"}:
        raise ValueError("task must be finqa or citation_support")
    rows = _read_suite(dataset_path, task)
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
        "device_map": "mps" if torch.backends.mps.is_available() else "auto",
    }
    if config.model.attn_implementation:
        model_kwargs["attn_implementation"] = config.model.attn_implementation
    base_model = AutoModelForCausalLM.from_pretrained(config.model.model_id, **model_kwargs)
    if adapter_directory is None:
        model = base_model
    else:
        from peft import PeftModel

        model = PeftModel.from_pretrained(base_model, adapter_directory, is_trainable=False)
    model.eval()
    output_rows: list[dict[str, Any]] = []
    batch_size = config.training.evaluation_batch_size
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        prompts = [
            tokenizer.apply_chat_template(
                row["messages"], tokenize=False, add_generation_prompt=True
            )
            for row in batch
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
            generated[:, inputs["input_ids"].shape[1] :], skip_special_tokens=True
        )
        for row, prediction in zip(batch, predictions, strict=True):
            try:
                parsed = json.loads(prediction.strip())
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if task == "finqa":
                value = parsed.get("answer") if isinstance(parsed, dict) else prediction.strip()
                output_rows.append(
                    {"id": row["id"], "prediction": value, "answer": row["answer"]}
                )
            else:
                expected = row["expected"]
                supported = bool(
                    isinstance(parsed, dict)
                    and parsed.get("concept") == expected["concept"]
                    and parsed.get("status") == expected["status"]
                    and parsed.get("value") is None
                )
                confidence = parsed.get("confidence") if isinstance(parsed, dict) else None
                if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
                    confidence = 1.0
                output_rows.append(
                    {"id": row["id"], "supported": supported, "confidence": confidence}
                )
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    with predictions_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in output_rows:
            stream.write(_canonical_json(row) + "\n")
    if task == "finqa":
        return {"finqa_accuracy": finqa_accuracy(output_rows)}
    return {"confident_hallucination_rate": confident_hallucination_rate(output_rows)}
