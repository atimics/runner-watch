from __future__ import annotations

import argparse
import json
from pathlib import Path

from sec_qwen.baseline import (
    evaluate_benchmark,
    prepare_citation_support,
    prepare_finqa,
)
from sec_qwen.benchmarks import release_metrics
from sec_qwen.completion import build_completion
from sec_qwen.config import load_config, validate_corpus
from sec_qwen.evaluation import evaluate_model, score_predictions
from sec_qwen.profiling import profile_corpus, write_profile
from sec_qwen.training import train


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_predictions(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Reproducible SEC Qwen LoRA harness")
    commands = parser.add_subparsers(dest="command", required=True)
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("config", type=Path)
    train_parser = commands.add_parser("train")
    train_parser.add_argument("config", type=Path)
    calibration_parser = commands.add_parser("calibrate")
    calibration_parser.add_argument("config", type=Path)
    calibration_parser.add_argument("--sample-fraction", type=float, default=0.01)
    calibration_parser.add_argument("--output", type=Path, required=True)
    profile_parser = commands.add_parser("profile")
    profile_parser.add_argument("config", type=Path)
    profile_parser.add_argument("--tokens-per-gpu-hour", type=float)
    profile_parser.add_argument("--gpu-hour-price", type=float)
    profile_parser.add_argument("--overhead-fraction", type=float, default=0.25)
    profile_parser.add_argument("--output", type=Path)
    evaluate_parser = commands.add_parser("evaluate")
    evaluate_parser.add_argument("config", type=Path)
    evaluate_parser.add_argument("--adapter", type=Path)
    evaluate_parser.add_argument("--split", required=True)
    evaluate_parser.add_argument("--predictions", type=Path, required=True)
    score_parser = commands.add_parser("score")
    score_parser.add_argument("predictions", type=Path)
    release_parser = commands.add_parser("release-score")
    release_parser.add_argument("--sec-predictions", type=Path, required=True)
    release_parser.add_argument("--candidate-finqa", type=Path, required=True)
    release_parser.add_argument("--baseline-finqa", type=Path, required=True)
    release_parser.add_argument("--candidate-hallucination", type=Path, required=True)
    release_parser.add_argument("--baseline-hallucination", type=Path, required=True)
    release_parser.add_argument("--finqa-margin-pp", type=float, default=8.0)
    release_parser.add_argument("--minimum-sec-field-exact-rate", type=float, default=0.70)
    finqa_parser = commands.add_parser("prepare-finqa")
    finqa_parser.add_argument("source", type=Path)
    finqa_parser.add_argument("output", type=Path)
    finqa_parser.add_argument("--source-revision", required=True)
    finqa_parser.add_argument("--limit", type=int)
    citation_parser = commands.add_parser("prepare-citation-support")
    citation_parser.add_argument("config", type=Path)
    citation_parser.add_argument("output", type=Path)
    benchmark_parser = commands.add_parser("benchmark-evaluate")
    benchmark_parser.add_argument("config", type=Path)
    benchmark_parser.add_argument("--dataset", type=Path, required=True)
    benchmark_parser.add_argument("--task", choices=("finqa", "citation_support"), required=True)
    benchmark_parser.add_argument("--predictions", type=Path, required=True)
    benchmark_parser.add_argument("--adapter", type=Path)
    completion_parser = commands.add_parser("completion")
    completion_parser.add_argument("--dispatch-ref", required=True)
    completion_parser.add_argument("--executor-id", required=True)
    completion_parser.add_argument("--run-id", required=True)
    completion_parser.add_argument("--metrics", type=Path, required=True)
    completion_parser.add_argument("--artifact", type=Path, required=True)
    completion_parser.add_argument("--artifact-uri", required=True)
    completion_parser.add_argument("--provider-version", required=True)
    completion_parser.add_argument("--completed-at-ms", type=int, required=True)
    completion_parser.add_argument("--exit-code", type=int, default=0)
    completion_parser.add_argument("--timed-out", action="store_true")
    arguments = parser.parse_args()

    if arguments.command == "score":
        print(_json({"metrics": score_predictions(_read_predictions(arguments.predictions))}))
        return
    if arguments.command == "release-score":
        metrics = release_metrics(
            sec_predictions=arguments.sec_predictions,
            candidate_finqa=arguments.candidate_finqa,
            baseline_finqa=arguments.baseline_finqa,
            candidate_hallucination=arguments.candidate_hallucination,
            baseline_hallucination=arguments.baseline_hallucination,
            finqa_margin_pp=arguments.finqa_margin_pp,
            minimum_sec_field_exact_rate=arguments.minimum_sec_field_exact_rate,
        )
        print(_json({"metrics": metrics}))
        return
    if arguments.command == "completion":
        completion = build_completion(
            dispatch_ref=arguments.dispatch_ref,
            executor_id=arguments.executor_id,
            run_id=arguments.run_id,
            metrics_path=arguments.metrics,
            artifact_path=arguments.artifact,
            artifact_uri=arguments.artifact_uri,
            provider_version=arguments.provider_version,
            completed_at_ms=arguments.completed_at_ms,
            exit_code=arguments.exit_code,
            timed_out=arguments.timed_out,
        )
        print(_json(completion))
        return
    if arguments.command == "prepare-finqa":
        print(
            _json(
                prepare_finqa(
                    arguments.source,
                    arguments.output,
                    source_revision=arguments.source_revision,
                    limit=arguments.limit,
                )
            )
        )
        return
    config = load_config(arguments.config)
    if arguments.command == "prepare-citation-support":
        print(_json(prepare_citation_support(config, arguments.output)))
        return
    if arguments.command == "benchmark-evaluate":
        print(
            _json(
                {
                    "metrics": evaluate_benchmark(
                        config,
                        dataset_path=arguments.dataset,
                        task=arguments.task,
                        predictions_path=arguments.predictions,
                        adapter_directory=arguments.adapter,
                    )
                }
            )
        )
        return
    if arguments.command == "validate":
        manifest = validate_corpus(config)
        print(_json({"corpus_id": manifest["id"], "valid": True}))
    elif arguments.command == "profile":
        profile = profile_corpus(
            config,
            tokens_per_gpu_hour=arguments.tokens_per_gpu_hour,
            gpu_hour_price=arguments.gpu_hour_price,
            overhead_fraction=arguments.overhead_fraction,
        )
        if arguments.output:
            write_profile(arguments.output, profile)
        print(_json(profile))
    elif arguments.command == "train":
        print(_json(train(config)))
    elif arguments.command == "calibrate":
        print(
            _json(
                train(
                    config,
                    sample_fraction=arguments.sample_fraction,
                    calibration=True,
                    output_directory=arguments.output,
                )
            )
        )
    elif arguments.command == "evaluate":
        metrics = evaluate_model(
            config,
            adapter_directory=arguments.adapter,
            split_file=arguments.split,
            predictions_path=arguments.predictions,
        )
        print(_json({"metrics": metrics}))


if __name__ == "__main__":
    main()
