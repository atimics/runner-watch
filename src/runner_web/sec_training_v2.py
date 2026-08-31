from __future__ import annotations

import argparse
import gzip
import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from runner_web import db
from runner_web.sec_training import (
    SPLIT_FILES,
    SYSTEM_PROMPT,
    TARGET_FIELDS,
    ExportResult,
    _canonical_json,
    _filing_prefix,
    _sha256,
    _split_examples,
    _target,
)

V2_SYSTEM_PROMPT = (
    SYSTEM_PROMPT
    + " Cite the supplied accession, source hash, and evidence span when those fields "
    "are requested."
)
BLOCK_TAGS = {
    "article",
    "br",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "p",
    "section",
    "table",
    "td",
    "th",
    "tr",
}
HEADING_RE = re.compile(
    r"^(?:item\s+[0-9]+[a-z]?(?:\.|\s|$)|part\s+[ivx]+(?:\.|\s|$)|"
    r"signatures?$|risk factors?$|management(?:'s)? discussion)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    source_url: str
    source_sha256: str
    content_type: str
    index: int
    section: str
    normalized_char_start: int
    normalized_char_end: int
    text: str
    sha256: str


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self.hidden_depth += 1
        elif tag in BLOCK_TAGS and self.hidden_depth == 0:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden_depth:
            self.hidden_depth -= 1
        elif tag in BLOCK_TAGS and self.hidden_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.hidden_depth == 0:
            self.parts.append(data)


def _clean_document(text: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(text)
        extracted = "".join(parser.parts)
    except Exception:
        extracted = text
    lines = [re.sub(r"\s+", " ", line).strip() for line in extracted.splitlines()]
    return "\n\n".join(line for line in lines if line)


def _split_long_paragraph(paragraph: str, maximum: int) -> list[str]:
    if len(paragraph) <= maximum:
        return [paragraph]
    parts: list[str] = []
    remaining = paragraph
    while len(remaining) > maximum:
        split_at = remaining.rfind(" ", 0, maximum + 1)
        if split_at < maximum // 2:
            split_at = maximum
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def semantic_chunks(
    text: str,
    *,
    source_url: str,
    source_sha256: str,
    content_type: str,
    target_chars: int = 6_000,
    max_chars: int = 8_000,
) -> list[DocumentChunk]:
    if target_chars < 256 or max_chars < target_chars:
        raise ValueError("chunk character limits are invalid")
    clean = _clean_document(text)
    paragraphs = [
        part
        for paragraph in clean.split("\n\n")
        for part in _split_long_paragraph(paragraph, max_chars)
        if part
    ]
    if not paragraphs:
        return []
    output: list[DocumentChunk] = []
    current: list[str] = []
    current_start = 0
    cursor = 0
    section = "document"
    current_section = section

    def flush() -> None:
        nonlocal current, current_start, current_section
        if not current:
            return
        chunk_text = "\n\n".join(current)
        output.append(
            DocumentChunk(
                source_url=source_url,
                source_sha256=source_sha256,
                content_type=content_type,
                index=len(output),
                section=current_section,
                normalized_char_start=current_start,
                normalized_char_end=current_start + len(chunk_text),
                text=chunk_text,
                sha256=hashlib.sha256(chunk_text.encode()).hexdigest(),
            )
        )
        current = []

    for paragraph in paragraphs:
        is_heading = bool(HEADING_RE.match(paragraph[:120])) and len(paragraph) <= 220
        next_section = paragraph[:160] if is_heading else section
        added = len(paragraph) + (2 if current else 0)
        section_changed = is_heading and current and next_section != current_section
        if current and (sum(map(len, current)) + added > target_chars or section_changed):
            flush()
            current_start = cursor
        if is_heading:
            section = next_section
        if not current:
            current_start = cursor
            current_section = section
        current.append(paragraph)
        cursor += len(paragraph) + 2
    flush()
    return output


def _decode_document(row: dict[str, Any], max_chars: int) -> str:
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
    return body.decode("utf-8", errors="replace")[:max_chars]


def _document_chunks(
    database: Any,
    filing_url: str,
    *,
    max_document_chars: int,
    max_documents: int,
    max_chunks: int,
) -> list[DocumentChunk]:
    rows = database.execute(
        """
        SELECT source_url,content_hash,content_type,content_encoding,content
        FROM source_documents
        WHERE source='sec' AND (source_url=? OR source_url LIKE ?)
        ORDER BY CASE
          WHEN source_url=? THEN 0
          WHEN source_url LIKE '%.txt' THEN 1
          WHEN source_url LIKE '%.htm%' THEN 2
          ELSE 3
        END,first_collected_at,content_hash
        LIMIT ?
        """,
        (filing_url, _filing_prefix(filing_url), filing_url, max_documents),
    ).fetchall()
    chunks: list[DocumentChunk] = []
    for raw in rows:
        row = dict(raw)
        text = _decode_document(row, max_document_chars)
        if not text:
            continue
        chunks.extend(
            semantic_chunks(
                text,
                source_url=str(row["source_url"]),
                source_sha256=str(row["content_hash"]),
                content_type=str(row["content_type"] or "application/octet-stream"),
            )
        )
        if len(chunks) >= max_chunks:
            break
    return chunks[:max_chunks]


def _facts(database: Any, cik: int, filed_at: str, limit: int) -> list[dict[str, Any]]:
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


def _source(chunk: DocumentChunk) -> dict[str, Any]:
    return {
        "source_url": chunk.source_url,
        "source_sha256": chunk.source_sha256,
        "content_type": chunk.content_type,
        "chunk_index": chunk.index,
        "section": chunk.section,
        "normalized_char_start": chunk.normalized_char_start,
        "normalized_char_end": chunk.normalized_char_end,
        "chunk_sha256": chunk.sha256,
    }


def _example(
    filing: dict[str, Any],
    *,
    task: str,
    suffix: str,
    evidence: dict[str, Any],
    answer: dict[str, Any],
    instruction: str,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "stonks.sec_chat_example.v2",
        "id": f"sec:{filing['accession']}:{task}:{suffix}",
        "task": task,
        "as_of": filing["filed_at"],
        "issuer_key": f"cik:{int(filing['cik'])}",
        "source": {
            "accession": filing["accession"],
            "filing_url": filing["filing_url"],
            "evidence": sources,
        },
        "messages": [
            {"role": "system", "content": V2_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"{instruction}\nEVIDENCE\n{_canonical_json(evidence)}",
            },
            {"role": "assistant", "content": _canonical_json(answer)},
        ],
    }


def _fact_comparisons(facts: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fact in facts:
        grouped.setdefault((str(fact["concept"]), str(fact["unit"])), []).append(fact)
    output: list[dict[str, Any]] = []
    for (concept, unit), rows in sorted(grouped.items()):
        unique: list[dict[str, Any]] = []
        seen_periods: set[str] = set()
        for row in rows:
            period = str(row["period_end"])
            if period not in seen_periods:
                unique.append(row)
                seen_periods.add(period)
        if len(unique) < 2:
            continue
        current, prior = unique[0], unique[1]
        current_value = float(current["value"])
        prior_value = float(prior["value"])
        absolute_change = current_value - prior_value
        output.append(
            {
                "concept": concept,
                "unit": unit,
                "prior_period_end": prior["period_end"],
                "prior_value": prior_value,
                "current_period_end": current["period_end"],
                "current_value": current_value,
                "absolute_change": absolute_change,
                "percent_change": absolute_change / abs(prior_value) * 100
                if prior_value
                else None,
            }
        )
        if len(output) >= limit:
            break
    return output


def _filing_examples(
    database: Any,
    filing: dict[str, Any],
    *,
    max_document_chars: int,
    max_documents: int,
    max_chunks: int,
    max_facts: int,
    max_examples: int,
) -> list[dict[str, Any]]:
    chunks = _document_chunks(
        database,
        str(filing["filing_url"]),
        max_document_chars=max_document_chars,
        max_documents=max_documents,
        max_chunks=max_chunks,
    )
    facts = _facts(database, int(filing["cik"]), str(filing["filed_at"]), max_facts)
    filing_identity = {
        key: filing[key]
        for key in ("accession", "cik", "ticker", "company", "form", "filed_at", "filing_url")
    }
    chunk_sources = [_source(chunk) for chunk in chunks]
    classification_evidence = {
        key: filing.get(key)
        for key in (
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
        if filing.get(key) is not None
    }
    preview = [
        {"source": _source(chunk), "text": chunk.text}
        for chunk in chunks[:1]
    ]
    examples = [
        _example(
            filing,
            task="sec_filing_structured_analysis",
            suffix="0",
            evidence={"filing": filing_identity, "facts": facts, "document_chunks": preview},
            answer=_target(filing),
            instruction=(
                "Return the requested structured filing fields: " + ", ".join(TARGET_FIELDS)
            ),
            sources=chunk_sources[:2],
        ),
        _example(
            filing,
            task="filing_classification",
            suffix="0",
            evidence={
                "filing": filing_identity,
                "deterministic_parser_evidence": classification_evidence,
            },
            answer={
                "form": filing["form"],
                "filing_kind": filing["kind"],
                "sentiment": filing["sentiment"],
                "score": float(filing["score"]),
            },
            instruction="Classify this filing using only the supplied SEC evidence.",
            sources=[],
        ),
    ]
    for chunk in chunks:
        citation = _source(chunk)
        examples.append(
            _example(
                filing,
                task="evidence_navigation",
                suffix=str(chunk.index),
                evidence={"filing": filing_identity, "document_chunk": chunk.text},
                answer={"accession": filing["accession"], **citation},
                instruction="Identify and cite the exact supplied SEC evidence span.",
                sources=[citation],
            )
        )
    for index in range(0, min(len(facts), 32), 8):
        group = facts[index : index + 8]
        examples.append(
            _example(
                filing,
                task="xbrl_fact_extraction",
                suffix=str(index // 8),
                evidence={"filing": filing_identity, "issuer_facts": group},
                answer={"accession": filing["accession"], "facts": group},
                instruction="Return the supplied point-in-time XBRL facts without changing values.",
                sources=[],
            )
        )
    for index, comparison in enumerate(_fact_comparisons(facts)):
        comparison_facts = [
            fact
            for fact in facts
            if fact["concept"] == comparison["concept"]
            and fact["unit"] == comparison["unit"]
            and fact["period_end"]
            in {comparison["prior_period_end"], comparison["current_period_end"]}
        ][:2]
        examples.append(
            _example(
                filing,
                task="fact_comparison",
                suffix=str(index),
                evidence={"filing": filing_identity, "facts": comparison_facts},
                answer=comparison,
                instruction="Compare the two latest distinct periods for the requested concept.",
                sources=[],
            )
        )
    known = {str(fact["concept"]) for fact in facts}
    missing = next(
        (
            concept
            for concept in (
                "cash",
                "debt_total",
                "shares_outstanding",
                "operating_cash_flow",
                "stockholders_equity",
            )
            if concept not in known
        ),
        None,
    )
    if missing:
        fact_inventory = sorted(
            {
                (
                    str(fact["concept"]),
                    str(fact["unit"]),
                    str(fact["accession"]),
                )
                for fact in facts
            }
        )
        examples.append(
            _example(
                filing,
                task="insufficient_evidence",
                suffix=missing,
                evidence={
                    "filing": filing_identity,
                    "available_fact_inventory": [
                        {"concept": concept, "unit": unit, "accession": accession}
                        for concept, unit, accession in fact_inventory
                    ],
                },
                answer={"concept": missing, "status": "insufficient_evidence", "value": None},
                instruction=f"Return the supported value for {missing}, or mark it insufficient.",
                sources=[],
            )
        )
    return examples[:max_examples]


def _balanced_filings(filings: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    groups: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for filing in reversed(filings):
        cik = int(filing["cik"])
        bucket = str(filing["form"]).split("/", 1)[0]
        groups.setdefault(cik, {}).setdefault(bucket, []).append(filing)
    selected: list[dict[str, Any]] = []
    for cik in sorted(groups):
        issuer_groups = groups[cik]
        issuer_rows: list[dict[str, Any]] = []
        while len(issuer_rows) < limit and issuer_groups:
            for bucket in sorted(tuple(issuer_groups)):
                issuer_rows.append(issuer_groups[bucket].pop(0))
                if not issuer_groups[bucket]:
                    del issuer_groups[bucket]
                if len(issuer_rows) >= limit:
                    break
        selected.extend(issuer_rows)
    return sorted(selected, key=lambda filing: (filing["filed_at"], filing["accession"]))


def export_sec_training_corpus_v2(
    output_directory: Path,
    *,
    repository: str,
    revision: str,
    source_path: str,
    dataset_id: str = "dataset://stonks/feral-7b-sec/v2",
    title: str = "FERAL-7B deterministic SEC multitask corpus v2",
    max_document_chars: int = 240_000,
    max_documents: int = 4,
    max_chunks_per_accession: int = 8,
    max_facts: int = 128,
    max_examples_per_accession: int = 20,
    max_filings_per_issuer: int = 32,
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
    if min(
        max_document_chars,
        max_documents,
        max_chunks_per_accession,
        max_examples_per_accession,
        max_filings_per_issuer,
    ) < 1 or max_facts < 0:
        raise ValueError("document, chunk, example, and fact limits are invalid")
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
        filings = [dict(row) for row in rows]
        filings = _balanced_filings(filings, max_filings_per_issuer)
        examples = [
            example
            for filing in filings
            for example in _filing_examples(
                database,
                filing,
                max_document_chars=max_document_chars,
                max_documents=max_documents,
                max_chunks=max_chunks_per_accession,
                max_facts=max_facts,
                max_examples=max_examples_per_accession,
            )
        ]
    if not examples:
        raise ValueError("no SEC filings are available to export")

    splits = _split_examples(examples, unseen_issuer_fraction)
    accession_splits: dict[str, str] = {}
    for split, records in splits.items():
        for example in records:
            accession = str(example["source"]["accession"])
            existing = accession_splits.setdefault(accession, split)
            if existing != split:
                raise RuntimeError(f"accession {accession} leaked across corpus splits")

    output_directory.mkdir(parents=True, exist_ok=True)
    for split, filename in SPLIT_FILES.items():
        with (output_directory / filename).open("w", encoding="utf-8", newline="\n") as stream:
            for example in splits[split]:
                stream.write(_canonical_json(example) + "\n")

    counts = {split: len(records) for split, records in splits.items()}
    task_counts = Counter(example["task"] for example in examples)
    character_count = sum(
        len(message["content"])
        for example in examples
        for message in example["messages"]
    )
    summary = {
        "schema": "stonks.sec_corpus_summary.v2",
        "filings": len(filings),
        "examples": len(examples),
        "issuers": len({example["issuer_key"] for example in examples}),
        "document_chunks": task_counts["evidence_navigation"],
        "estimated_training_tokens": character_count // 4,
        "task_counts": dict(sorted(task_counts.items())),
        "split_counts": counts,
        "split_policy": "accession-locked-issuer-hash-unseen-then-time-groups-v2",
        "target_policy": "deterministic-sec-multitask-no-teacher-no-market-outcomes-v2",
    }
    summary_path = output_directory / "dataset-summary.json"
    summary_path.write_text(_canonical_json(summary) + "\n", encoding="utf-8", newline="\n")

    files: list[dict[str, Any]] = []
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
                "preserve accession numbers, source URLs, hashes, and evidence spans",
                "not investment advice",
            ],
        },
        "files": files,
        "metadata": {
            "example_schema": "stonks.sec_chat_example.v2",
            "filings": str(len(filings)),
            "examples": str(len(examples)),
            "issuers": str(summary["issuers"]),
            "estimated_training_tokens": str(summary["estimated_training_tokens"]),
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
    parser = argparse.ArgumentParser(description="Export the FERAL-7B SEC multitask corpus v2")
    parser.add_argument("output", type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-path", default="exports/feral-7b-sec-v2")
    parser.add_argument("--dataset-id", default="dataset://stonks/feral-7b-sec/v2")
    parser.add_argument("--title", default="FERAL-7B deterministic SEC multitask corpus v2")
    parser.add_argument("--max-document-chars", type=int, default=240_000)
    parser.add_argument("--max-documents", type=int, default=4)
    parser.add_argument("--max-chunks-per-accession", type=int, default=8)
    parser.add_argument("--max-facts", type=int, default=128)
    parser.add_argument("--max-examples-per-accession", type=int, default=20)
    parser.add_argument("--max-filings-per-issuer", type=int, default=32)
    parser.add_argument("--unseen-issuer-fraction", type=float, default=0.10)
    parser.add_argument("--database-path", type=Path)
    arguments = parser.parse_args()
    if arguments.database_path:
        if db.DATABASE_URL:
            parser.error("--database-path cannot be combined with DATABASE_URL")
        db.DATABASE_PATH = arguments.database_path
    result = export_sec_training_corpus_v2(
        arguments.output,
        repository=arguments.repository,
        revision=arguments.revision,
        source_path=arguments.source_path,
        dataset_id=arguments.dataset_id,
        title=arguments.title,
        max_document_chars=arguments.max_document_chars,
        max_documents=arguments.max_documents,
        max_chunks_per_accession=arguments.max_chunks_per_accession,
        max_facts=arguments.max_facts,
        max_examples_per_accession=arguments.max_examples_per_accession,
        max_filings_per_issuer=arguments.max_filings_per_issuer,
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
