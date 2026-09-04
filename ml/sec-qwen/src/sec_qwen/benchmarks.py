from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sec_qwen.evaluation import score_predictions


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _answer(value: Any) -> str:
    text = str(value).strip().lower().replace(",", "").replace("$", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return format(Decimal(text).normalize(), "f")
    except InvalidOperation:
        return " ".join(text.split())


def _aligned(left: list[dict[str, Any]], right: list[dict[str, Any]], label: str) -> None:
    left_ids = [str(row.get("id") or "") for row in left]
    right_ids = [str(row.get("id") or "") for row in right]
    if not left_ids or left_ids != right_ids or len(set(left_ids)) != len(left_ids):
        raise ValueError(f"{label} candidate and baseline rows must have identical unique IDs")


def finqa_accuracy(rows: list[dict[str, Any]]) -> float:
    if not rows:
        raise ValueError("FinQA rows must not be empty")
    correct = sum(_answer(row.get("prediction")) == _answer(row.get("answer")) for row in rows)
    return correct / len(rows)


def confident_hallucination_rate(rows: list[dict[str, Any]], threshold: float = 0.8) -> float:
    if not rows:
        raise ValueError("hallucination rows must not be empty")
    failures = 0
    for row in rows:
        supported = row.get("supported")
        confidence = row.get("confidence")
        if not isinstance(supported, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("hallucination rows require boolean supported and numeric confidence")
        if not 0 <= float(confidence) <= 1:
            raise ValueError("hallucination confidence must be between 0 and 1")
        failures += int(not supported and float(confidence) >= threshold)
    return failures / len(rows)


def release_metrics(
    *,
    sec_predictions: Path,
    candidate_finqa: Path,
    baseline_finqa: Path,
    candidate_hallucination: Path,
    baseline_hallucination: Path,
    finqa_margin_pp: float = 8.0,
    minimum_sec_field_exact_rate: float = 0.70,
) -> dict[str, float]:
    sec_rows = _read_jsonl(sec_predictions)
    candidate_finqa_rows = _read_jsonl(candidate_finqa)
    baseline_finqa_rows = _read_jsonl(baseline_finqa)
    candidate_hallucination_rows = _read_jsonl(candidate_hallucination)
    baseline_hallucination_rows = _read_jsonl(baseline_hallucination)
    _aligned(candidate_finqa_rows, baseline_finqa_rows, "FinQA")
    _aligned(candidate_hallucination_rows, baseline_hallucination_rows, "hallucination")
    sec_metrics = score_predictions(sec_rows)
    finqa_delta = 100 * (finqa_accuracy(candidate_finqa_rows) - finqa_accuracy(baseline_finqa_rows))
    hallucination_delta = 100 * (
        confident_hallucination_rate(candidate_hallucination_rows)
        - confident_hallucination_rate(baseline_hallucination_rows)
    )
    gate = (
        finqa_delta >= finqa_margin_pp
        and hallucination_delta <= 0
        and sec_metrics["sec_field_exact_rate"] >= minimum_sec_field_exact_rate
    )
    return {
        "confident_hallucination_rate_delta_pp": hallucination_delta,
        "feral_release_gate": float(gate),
        "finqa_accuracy_delta_pp": finqa_delta,
        "sec_field_exact_rate": sec_metrics["sec_field_exact_rate"],
    }
