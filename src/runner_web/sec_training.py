from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner_web import db

SPLIT_FILES = {
    "train": "train.jsonl",
    "validation": "validation.jsonl",
    "test_future": "test-future.jsonl",
    "test_unseen_issuer": "test-unseen-issuer.jsonl",
}
SYSTEM_PROMPT = (
    "You analyze SEC filings. Use only the supplied filing and facts. "
    "Return one compact JSON object with exactly the requested fields. "
    "Do not give trading advice and do not invent missing values."
)
TARGET_FIELDS = (
    "accession",
    "cik",
    "ticker",
    "form",
    "filing_kind",
    "sentiment",
    "score",
    "title",
    "actor",
    "actor_title",
    "transaction_codes",
    "transaction_shares",
    "transaction_price",
    "transaction_value",
    "post_transaction_shares",
    "stake_change_pct",
    "is_10b5_1",
    "direct_ownership",
    "beneficial_ownership_pct",
)


@dataclass(frozen=True)
class ExportResult:
    output_directory: Path
    manifest_path: Path
    manifest_sha256: str
    split_counts: dict[str, int]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _filing_prefix(url: str) -> str:
    return f"{url.rsplit('/', 1)[0]}/%" if "/" in url else url


def _document_text(row: dict[str, Any]) -> str:
    body = row.get("content")
    if isinstance(body, memoryview):
        body = body.tobytes()
    if not isinstance(body, bytes):
        return ""
    if str(row.get("content_encoding") or "identity") == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            return ""
    if hashlib.sha256(body).hexdigest() != str(row.get("content_hash") or ""):
        return ""
    return body.decode("utf-8", errors="replace").strip()


def _filing_documents(
    database: Any,
    filing_url: str,
    *,
    max_document_chars: int,
) -> tuple[str, list[dict[str, Any]]]:
    rows = database.execute(
        """
        SELECT source_url,content_hash,content_type,content_encoding,content,
               first_collected_at,last_collected_at
        FROM source_documents
        WHERE source_url=? OR source_url LIKE ?
        ORDER BY CASE
          WHEN source_url=? THEN 0
          WHEN source_url LIKE '%.txt' THEN 1
          WHEN source_url LIKE '%.htm%' THEN 2
          ELSE 3
        END,first_collected_at,content_hash
        LIMIT 8
        """,
        (filing_url, _filing_prefix(filing_url), filing_url),
    ).fetchall()
    parts: list[str] = []
    sources: list[dict[str, Any]] = []
    remaining = max_document_chars
    for raw in rows:
        row = dict(raw)
        text = _document_text(row)
        if not text or remaining <= 0:
            continue
        excerpt = text[:remaining]
        parts.append(f"SOURCE {row['source_url']}\n{excerpt}")
        sources.append(
            {
                "source_url": row["source_url"],
                "content_hash": row["content_hash"],
                "content_type": row["content_type"],
                "first_collected_at": row["first_collected_at"],
                "included_characters": len(excerpt),
            }
        )
        remaining -= len(excerpt)
    return "\n\n".join(parts), sources


def _facts_as_of(database: Any, cik: int, filed_at: str, limit: int) -> list[dict[str, Any]]:
    rows = database.execute(
        """
        SELECT concept,value,unit,period_start,period_end,filed_at,accession,form,source_tag
        FROM issuer_facts
        WHERE cik=? AND filed_at<=?
        ORDER BY filed_at DESC,period_end DESC,concept,accession
        LIMIT ?
        """,
        (cik, filed_at, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _target(filing: dict[str, Any]) -> dict[str, Any]:
    target = {
        field: filing.get("kind" if field == "filing_kind" else field) for field in TARGET_FIELDS
    }
    target["cik"] = int(target["cik"])
    target["score"] = float(target["score"])
    if target["is_10b5_1"] is not None:
        target["is_10b5_1"] = bool(target["is_10b5_1"])
    return target


def _example(
    database: Any,
    filing: dict[str, Any],
    *,
    max_document_chars: int,
    max_facts: int,
) -> dict[str, Any]:
    document, documents = _filing_documents(
        database,
        str(filing["filing_url"]),
        max_document_chars=max_document_chars,
    )
    facts = _facts_as_of(database, int(filing["cik"]), str(filing["filed_at"]), max_facts)
    evidence = {
        "filing": {
            "accession": filing["accession"],
            "cik": int(filing["cik"]),
            "ticker": filing["ticker"],
            "company": filing["company"],
            "form": filing["form"],
            "filed_at": filing["filed_at"],
            "filing_url": filing["filing_url"],
        },
        "issuer_facts_available_as_of_filing": facts,
        "archived_filing_text": document or None,
    }
    return {
        "schema": "stonks.sec_chat_example.v1",
        "id": f"sec:{filing['accession']}",
        "task": "sec_filing_structured_analysis",
        "as_of": filing["filed_at"],
        "issuer_key": f"cik:{int(filing['cik'])}",
        "source": {
            "accession": filing["accession"],
            "filing_url": filing["filing_url"],
            "documents": documents,
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Analyze this point-in-time SEC evidence and return these fields: "
                    + ", ".join(TARGET_FIELDS)
                    + ".\nEVIDENCE\n"
                    + _canonical_json(evidence)
                ),
            },
            {"role": "assistant", "content": _canonical_json(_target(filing))},
        ],
    }


def _split_examples(
    examples: list[dict[str, Any]], unseen_issuer_fraction: float
) -> dict[str, list[dict[str, Any]]]:
    issuer_keys = sorted(
        {example["issuer_key"] for example in examples},
        key=lambda value: (hashlib.sha256(value.encode()).hexdigest(), value),
    )
    unseen_count = 0
    if len(issuer_keys) >= 3 and unseen_issuer_fraction > 0:
        unseen_count = min(
            len(issuer_keys) - 1,
            max(1, math.ceil(len(issuer_keys) * unseen_issuer_fraction)),
        )
    unseen = set(issuer_keys[:unseen_count])
    unseen_examples = [example for example in examples if example["issuer_key"] in unseen]
    seen_examples = [example for example in examples if example["issuer_key"] not in unseen]

    timestamps = sorted({str(example["as_of"]) for example in seen_examples})
    future_times: set[str] = set()
    validation_times: set[str] = set()
    if len(timestamps) >= 3:
        future_groups = max(1, math.ceil(len(timestamps) * 0.15))
        validation_groups = max(1, math.ceil(len(timestamps) * 0.10))
        overflow = future_groups + validation_groups - (len(timestamps) - 1)
        if overflow > 0:
            validation_groups = max(0, validation_groups - overflow)
        future_times = set(timestamps[-future_groups:])
        if validation_groups:
            validation_times = set(
                timestamps[-(future_groups + validation_groups) : -future_groups]
            )
    elif len(timestamps) == 2:
        future_times = {timestamps[-1]}

    splits = {name: [] for name in SPLIT_FILES}
    splits["test_unseen_issuer"] = unseen_examples
    for example in seen_examples:
        if example["as_of"] in future_times:
            split = "test_future"
        elif example["as_of"] in validation_times:
            split = "validation"
        else:
            split = "train"
        splits[split].append(example)
    for rows in splits.values():
        rows.sort(key=lambda example: (example["as_of"], example["id"]))
    return splits


def export_sec_training_corpus(
    output_directory: Path,
    *,
    repository: str,
    revision: str,
    source_path: str,
    dataset_id: str = "dataset://stonks/sec-filings-qwen/v1",
    title: str = "Runner Watch SEC filing chat corpus",
    max_document_chars: int = 24_000,
    max_facts: int = 64,
    unseen_issuer_fraction: float = 0.10,
) -> ExportResult:
    if not repository.startswith("https://"):
        raise ValueError("repository must be an https:// URL")
    if len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("revision must be a full lowercase Git commit SHA")
    if not dataset_id.startswith("dataset://") or any(
        character.isspace() for character in dataset_id
    ):
        raise ValueError("dataset_id must be a dataset:// handle")
    if output_directory.exists() and any(output_directory.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output_directory}")
    if max_document_chars < 1 or max_facts < 0:
        raise ValueError("document and fact limits must be non-negative")
    if not 0 <= unseen_issuer_fraction < 1:
        raise ValueError("unseen_issuer_fraction must be between 0 and 1")

    with db.connection() as database:
        rows = database.execute(
            """
            SELECT accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                   filing_url,actor,actor_title,transaction_codes,transaction_shares,
                   transaction_price,transaction_value,post_transaction_shares,
                   stake_change_pct,is_10b5_1,direct_ownership,beneficial_ownership_pct
            FROM sec_filings
            ORDER BY filed_at,accession
            """
        ).fetchall()
        examples = [
            _example(
                database,
                dict(row),
                max_document_chars=max_document_chars,
                max_facts=max_facts,
            )
            for row in rows
        ]
    if not examples:
        raise ValueError("no SEC filings are available to export")

    splits = _split_examples(examples, unseen_issuer_fraction)
    output_directory.mkdir(parents=True, exist_ok=True)
    for split, filename in SPLIT_FILES.items():
        path = output_directory / filename
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            for example in splits[split]:
                stream.write(_canonical_json(example))
                stream.write("\n")

    counts = {split: len(records) for split, records in splits.items()}
    summary = {
        "schema": "stonks.sec_corpus_summary.v1",
        "examples": len(examples),
        "examples_with_archived_documents": sum(
            bool(example["source"]["documents"]) for example in examples
        ),
        "issuers": len({example["issuer_key"] for example in examples}),
        "split_counts": counts,
        "split_policy": "issuer-hash-unseen-then-time-groups-v1",
        "target_policy": "existing-runner-parser-fields-no-market-outcomes-v1",
    }
    summary_path = output_directory / "dataset-summary.json"
    summary_path.write_text(_canonical_json(summary) + "\n", encoding="utf-8", newline="\n")

    files = []
    for filename in [*SPLIT_FILES.values(), summary_path.name]:
        path = output_directory / filename
        files.append(
            {
                "path": filename,
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
                "media_type": (
                    "application/x-jsonlines" if filename.endswith(".jsonl") else "application/json"
                ),
            }
        )
    manifest = {
        "schema": "ilxyr.corpus_release.v1",
        "id": dataset_id,
        "title": title,
        "version": f"git-{revision[:12]}",
        "source": {"repository": repository, "revision": revision, "path": source_path},
        "rights": {
            "license": "NOASSERTION",
            "use_constraints": [
                "SEC access terms and source document rights apply",
                "preserve accession numbers and source URLs",
                "not investment advice",
            ],
        },
        "files": files,
        "metadata": {
            "example_schema": "stonks.sec_chat_example.v1",
            "examples": str(len(examples)),
            "issuers": str(summary["issuers"]),
            "split_policy": summary["split_policy"],
            "target_policy": summary["target_policy"],
        },
    }
    manifest_path = output_directory / "corpus-release.json"
    manifest_path.write_text(_canonical_json(manifest) + "\n", encoding="utf-8", newline="\n")
    return ExportResult(
        output_directory=output_directory,
        manifest_path=manifest_path,
        manifest_sha256=_sha256(manifest_path),
        split_counts=counts,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Export a frozen SEC chat corpus for Qwen")
    parser.add_argument("output", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-path", default="exports/sec-qwen")
    parser.add_argument("--dataset-id", default="dataset://stonks/sec-filings-qwen/v1")
    parser.add_argument("--title", default="Runner Watch SEC filing chat corpus")
    parser.add_argument("--max-document-chars", type=int, default=24_000)
    parser.add_argument("--max-facts", type=int, default=64)
    parser.add_argument("--unseen-issuer-fraction", type=float, default=0.10)
    parser.add_argument("--database-path", type=Path)
    arguments = parser.parse_args()
    if arguments.database_path:
        if db.DATABASE_URL:
            parser.error("--database-path cannot be combined with DATABASE_URL")
        db.DATABASE_PATH = arguments.database_path
    result = export_sec_training_corpus(
        arguments.output,
        repository=arguments.repository,
        revision=arguments.revision,
        source_path=arguments.source_path,
        dataset_id=arguments.dataset_id,
        title=arguments.title,
        max_document_chars=arguments.max_document_chars,
        max_facts=arguments.max_facts,
        unseen_issuer_fraction=arguments.unseen_issuer_fraction,
    )
    print(
        _canonical_json(
            {
                "manifest": str(result.manifest_path),
                "manifest_sha256": result.manifest_sha256,
                "split_counts": result.split_counts,
            }
        )
    )


if __name__ == "__main__":
    main()
