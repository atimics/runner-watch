from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

from pytest import MonkeyPatch, raises

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.sec_training import SPLIT_FILES, export_sec_training_corpus

SEC_QWEN_SOURCE = Path(__file__).resolve().parents[1] / "ml" / "sec-qwen" / "src"
sys.path.insert(0, str(SEC_QWEN_SOURCE))

from sec_qwen.baseline import prepare_citation_support, prepare_finqa  # noqa: E402
from sec_qwen.benchmarks import release_metrics  # noqa: E402
from sec_qwen.completion import build_completion  # noqa: E402
from sec_qwen.config import load_config, load_examples, validate_corpus  # noqa: E402
from sec_qwen.evaluation import score_predictions  # noqa: E402
from sec_qwen.profiling import profile_corpus  # noqa: E402
from sec_qwen.training import _calibration_sample  # noqa: E402


def _insert_filing(
    database: object,
    *,
    accession: str,
    cik: int,
    ticker: str,
    filed_at: str,
) -> None:
    filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}.txt"
    database.execute(
        """
        INSERT INTO sec_filings(
            accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
            filing_url,actor,actor_title,transaction_codes,transaction_shares,
            transaction_price,transaction_value,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            accession,
            cik,
            ticker,
            f"{ticker} Corp",
            "4",
            "Insider purchase",
            "positive",
            72.5,
            f"Form 4 for {ticker}",
            filed_at,
            filing_url,
            "A Director",
            "Director",
            "P",
            1_000.0,
            2.5,
            2_500.0,
            filed_at,
            filed_at,
        ),
    )
    body = f"SEC filing {accession} for {ticker}. Transaction code P.".encode()
    digest = hashlib.sha256(body).hexdigest()
    stored = gzip.compress(body, mtime=0)
    database.execute(
        """
        INSERT INTO source_documents(
            source,source_url,content_hash,content_type,content_encoding,content,
            first_collected_at,last_collected_at
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        ("sec", filing_url, digest, "text/plain", "gzip", stored, filed_at, filed_at),
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
    )


def test_sec_export_is_deterministic_point_in_time_and_leakage_safe(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "sec-training.db")
    init_db()
    with connection() as database:
        database.execute(
            """
            INSERT INTO ingestion_runs(
                id,source,feed,locator,status,requested_count,received_count,
                metadata_json,started_at,finished_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "run-1",
                "sec",
                "companyfacts",
                "test",
                "success",
                2,
                2,
                "{}",
                "2024-01-01",
                "2030-01-01",
            ),
        )
        for issuer in range(1, 5):
            for period in range(1, 4):
                _insert_filing(
                    database,
                    accession=f"{issuer:02d}-{period:02d}",
                    cik=issuer,
                    ticker=f"T{issuer}",
                    filed_at=f"2026-0{period}-01T00:00:00+00:00",
                )
        for fact_id, filed_at, value in [
            ("fact-before", "2025-12-31T00:00:00+00:00", 10.0),
            ("fact-future", "2027-01-01T00:00:00+00:00", 99.0),
        ]:
            database.execute(
                """
                INSERT INTO issuer_facts(
                    id,source,feed,cik,concept,value,unit,period_end,filed_at,accession,
                    payload_json,first_run_id,last_run_id,first_collected_at,last_collected_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    fact_id,
                    "sec",
                    "companyfacts",
                    1,
                    "Cash",
                    value,
                    "USD",
                    "2025-12-31",
                    filed_at,
                    fact_id,
                    "{}",
                    "run-1",
                    "run-1",
                    filed_at,
                    filed_at,
                ),
            )

    arguments = {
        "repository": "https://github.com/atimics/runner-watch",
        "revision": "a" * 40,
        "source_path": "exports/sec-qwen-test",
        "unseen_issuer_fraction": 0.25,
    }
    first = export_sec_training_corpus(tmp_path / "first", **arguments)
    second = export_sec_training_corpus(tmp_path / "second", **arguments)
    assert first.manifest_sha256 == second.manifest_sha256
    for filename in [*SPLIT_FILES.values(), "dataset-summary.json", "corpus-release.json"]:
        assert (tmp_path / "first" / filename).read_bytes() == (
            tmp_path / "second" / filename
        ).read_bytes()

    splits = {
        name: _read_jsonl(tmp_path / "first" / filename)
        for name, filename in SPLIT_FILES.items()
    }
    all_examples = [example for rows in splits.values() for example in rows]
    assert len(all_examples) == 12
    assert len({example["id"] for example in all_examples}) == 12
    unseen_issuers = {example["issuer_key"] for example in splits["test_unseen_issuer"]}
    seen_issuers = {
        example["issuer_key"]
        for name, rows in splits.items()
        if name != "test_unseen_issuer"
        for example in rows
    }
    assert unseen_issuers
    assert unseen_issuers.isdisjoint(seen_issuers)
    assert max(example["as_of"] for example in splits["train"]) < min(
        example["as_of"] for example in splits["test_future"]
    )

    issuer_one = next(example for example in all_examples if example["issuer_key"] == "cik:1")
    evidence_text = issuer_one["messages"][1]["content"].split("\nEVIDENCE\n", 1)[1]
    evidence = json.loads(evidence_text)
    facts = evidence["issuer_facts_available_as_of_filing"]
    assert {fact["accession"] for fact in facts} == {"fact-before"}
    assert issuer_one["source"]["documents"][0]["content_hash"]


def test_qwen_config_verifies_corpus_and_scores_strict_json(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    names = ["train.jsonl", "validation.jsonl", "test-future.jsonl", "test-unseen-issuer.jsonl"]
    files = []
    for name in names:
        path = corpus / name
        path.write_text("", encoding="utf-8")
        files.append(
            {
                "path": name,
                "sha256": hashlib.sha256(b"").hexdigest(),
                "size_bytes": 0,
                "media_type": "application/x-jsonlines",
            }
        )
    manifest_path = corpus / "corpus-release.json"
    manifest_path.write_text(
        json.dumps({"schema": "ilxyr.corpus_release.v1", "id": "dataset://test", "files": files}),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f"""
schema = "stonks.sec_qwen_training.v1"
[model]
model_id = "Qwen/Qwen2.5-7B-Instruct"
revision = "{'b' * 40}"
[dataset]
corpus_manifest = "corpus/corpus-release.json"
train_file = "train.jsonl"
validation_file = "validation.jsonl"
evaluation_files = ["test-future.jsonl", "test-unseen-issuer.jsonl"]
[training]
target_modules = ["q_proj", "v_proj"]
[output]
directory = "output"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert validate_corpus(config)["id"] == "dataset://test"
    (corpus / "train.jsonl").write_text("tampered\n", encoding="utf-8")
    with raises(ValueError, match="hash does not match"):
        validate_corpus(config)

    target = json.dumps({"form": "4", "sentiment": "positive"}, sort_keys=True)
    metrics = score_predictions(
        [
            {"target": target, "prediction": target},
            {"target": target, "prediction": '{"form":"4","sentiment":"neutral"}'},
            {"target": target, "prediction": "not json"},
        ]
    )
    assert metrics == {
        "sec_example_exact_rate": 1 / 3,
        "sec_field_exact_rate": 0.5,
        "sec_json_valid_rate": 2 / 3,
    }


def test_qwen_loader_accepts_deterministic_v2_examples(tmp_path: Path) -> None:
    path = tmp_path / "v2.jsonl"
    example = {
        "schema": "stonks.sec_chat_example.v2",
        "messages": [
            {"role": "system", "content": "Use evidence."},
            {"role": "user", "content": "Classify."},
            {"role": "assistant", "content": '{"form":"10-K"}'},
        ],
    }
    _write_jsonl(path, [example])
    assert load_examples(path) == [example]


def test_qwen_profile_uses_exact_template_tokens_and_builds_budget(tmp_path: Path) -> None:
    corpus = tmp_path / "profile-corpus"
    corpus.mkdir()
    example = {
        "schema": "stonks.sec_chat_example.v2",
        "id": "sec:1:classification:0",
        "task": "filing_classification",
        "messages": [
            {"role": "system", "content": "Use evidence."},
            {"role": "user", "content": "Classify."},
            {"role": "assistant", "content": '{"form":"10-K"}'},
        ],
    }
    files = []
    for name, rows in {
        "train.jsonl": [example],
        "validation.jsonl": [example],
        "test-future.jsonl": [],
        "test-unseen-issuer.jsonl": [],
    }.items():
        path = corpus / name
        _write_jsonl(path, rows)
        files.append(
            {
                "path": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
        )
    manifest = corpus / "corpus-release.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "ilxyr.corpus_release.v1",
                "id": "dataset://profile",
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "profile.toml"
    config_path.write_text(
        f"""
schema = "stonks.sec_qwen_training.v1"
[model]
model_id = "Qwen/Qwen2.5-7B-Instruct"
revision = "{'d' * 40}"
[dataset]
corpus_manifest = "profile-corpus/corpus-release.json"
train_file = "train.jsonl"
validation_file = "validation.jsonl"
evaluation_files = ["test-future.jsonl", "test-unseen-issuer.jsonl"]
[training]
epochs = 2
max_seq_length = 256
gradient_accumulation_steps = 2
target_modules = ["q_proj"]
dataloader_num_workers = 4
evaluation_batch_size = 2
[output]
directory = "output"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    class CharacterTokenizer:
        def apply_chat_template(
            self,
            messages: list[dict[str, str]],
            *,
            tokenize: bool,
            add_generation_prompt: bool,
        ) -> str:
            assert not tokenize
            content = "|".join(message["content"] for message in messages)
            return content + ("|" if add_generation_prompt else "")

        def __call__(self, text: str, *, add_special_tokens: bool) -> dict[str, list[int]]:
            assert not add_special_tokens
            return {"input_ids": list(text.encode())}

    config = load_config(config_path)
    profile = profile_corpus(
        config,
        tokenizer=CharacterTokenizer(),
        tokens_per_gpu_hour=1_000,
        gpu_hour_price=4.0,
    )
    assert profile["splits"]["train.jsonl"]["examples"] == 1
    assert profile["splits"]["train.jsonl"]["supervised_tokens"] > 0
    assert profile["splits"]["train.jsonl"]["task_tokens"] == {
        "filing_classification": profile["splits"]["train.jsonl"]["effective_tokens"]
    }
    assert profile["training"]["optimizer_steps"] == 1
    assert profile["budget"]["recommended_cost_limit"] > 0


def test_feral_release_gate_and_ilxyr_completion(tmp_path: Path) -> None:
    target = json.dumps({"form": "4", "sentiment": "positive"}, sort_keys=True)
    sec_predictions = tmp_path / "sec.jsonl"
    _write_jsonl(sec_predictions, [{"target": target, "prediction": target}])

    candidate_finqa = tmp_path / "candidate-finqa.jsonl"
    baseline_finqa = tmp_path / "baseline-finqa.jsonl"
    _write_jsonl(
        candidate_finqa,
        [{"id": f"q-{index}", "prediction": int(index == 0), "answer": 1} for index in range(10)],
    )
    _write_jsonl(
        baseline_finqa,
        [{"id": f"q-{index}", "prediction": 0, "answer": 1} for index in range(10)],
    )
    candidate_hallucination = tmp_path / "candidate-hallucination.jsonl"
    baseline_hallucination = tmp_path / "baseline-hallucination.jsonl"
    hallucination_rows = [
        {"id": f"h-{index}", "supported": True, "confidence": 0.9} for index in range(2)
    ]
    _write_jsonl(candidate_hallucination, hallucination_rows)
    _write_jsonl(baseline_hallucination, hallucination_rows)

    metrics = release_metrics(
        sec_predictions=sec_predictions,
        candidate_finqa=candidate_finqa,
        baseline_finqa=baseline_finqa,
        candidate_hallucination=candidate_hallucination,
        baseline_hallucination=baseline_hallucination,
    )
    assert metrics == {
        "confident_hallucination_rate_delta_pp": 0.0,
        "feral_release_gate": 1.0,
        "finqa_accuracy_delta_pp": 10.0,
        "sec_field_exact_rate": 1.0,
    }
    metrics_path = tmp_path / "release-metrics.json"
    metrics_path.write_text(json.dumps({"metrics": metrics}), encoding="utf-8")
    adapter = tmp_path / "adapter.tar"
    adapter.write_bytes(b"adapter")
    completion = build_completion(
        dispatch_ref=f"artifact://sha256/{'a' * 64}",
        executor_id="service://cloud/gpu-1",
        run_id="run://feral-7b/test-1",
        metrics_path=metrics_path,
        artifact_path=adapter,
        artifact_uri="s3://feral-models/adapter.tar?versionId=exact",
        provider_version="exact",
        completed_at_ms=1_788_134_400_000,
    )
    assert completion["schema"] == "ilxyr.oci_job_completion.v1"
    assert completion["metrics"] == metrics
    assert completion["artifacts"][0]["sha256"] == hashlib.sha256(b"adapter").hexdigest()


def test_calibration_sample_is_stable_and_uses_one_percent_ceiling() -> None:
    examples = [{"id": f"example-{index}"} for index in range(101)]
    first = _calibration_sample(examples, 0.01)
    second = _calibration_sample(list(reversed(examples)), 0.01)
    assert first == second
    assert len(first) == 2


def test_prepare_finqa_freezes_source_and_output_digests(tmp_path: Path) -> None:
    source = tmp_path / "test.json"
    source.write_text(
        json.dumps(
            [
                {
                    "id": "report-1",
                    "pre_text": ["Revenue was 10."],
                    "post_text": ["Costs were 4."],
                    "table_ori": [["metric", "value"], ["revenue", "10"]],
                    "qa": {"question": "What is revenue?", "answer": "10"},
                }
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "finqa.jsonl"
    manifest = prepare_finqa(source, output, source_revision="f" * 40)
    rows = _read_jsonl(output)
    assert rows[0]["task"] == "finqa"
    assert rows[0]["answer"] == "10"
    assert manifest["examples"] == 1
    assert manifest["file"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_prepare_citation_support_uses_only_sealed_insufficient_cases(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    insufficient = {
        "schema": "stonks.sec_chat_example.v2",
        "id": "sec:1:insufficient:cash",
        "task": "insufficient_evidence",
        "messages": [
            {"role": "system", "content": "Use evidence."},
            {"role": "user", "content": "Return cash or mark it insufficient."},
            {
                "role": "assistant",
                "content": '{"concept":"cash","status":"insufficient_evidence","value":null}',
            },
        ],
    }
    files = []
    for name, rows in {
        "train.jsonl": [],
        "validation.jsonl": [],
        "test-future.jsonl": [insufficient],
        "test-unseen-issuer.jsonl": [],
    }.items():
        path = corpus / name
        _write_jsonl(path, rows)
        files.append(
            {
                "path": name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
        )
    manifest_path = corpus / "corpus-release.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "ilxyr.corpus_release.v1",
                "id": "dataset://citation-test",
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'''
schema = "stonks.sec_qwen_training.v1"
[model]
model_id = "Qwen/Qwen2.5-7B-Instruct"
revision = "{'a' * 40}"
[dataset]
corpus_manifest = "corpus/corpus-release.json"
train_file = "train.jsonl"
validation_file = "validation.jsonl"
evaluation_files = ["test-future.jsonl", "test-unseen-issuer.jsonl"]
[training]
target_modules = ["q_proj"]
[output]
directory = "output"
'''.strip()
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "citation.jsonl"
    result = prepare_citation_support(load_config(config_path), output)
    rows = _read_jsonl(output)
    assert result["examples"] == 1
    assert rows[0]["expected"] == {
        "concept": "cash",
        "status": "insufficient_evidence",
        "value": None,
    }
    assert "confidence" in rows[0]["messages"][0]["content"]
