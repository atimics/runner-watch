from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import math
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from collections.abc import Iterable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from runner_watch.edgar import (
    BeneficialOwnershipSummary,
    OwnershipSummary,
    classify_filing,
    parse_beneficial_ownership_xml,
    parse_ownership_xml,
)
from runner_watch.ingestion import IssuerFact
from runner_web.sec_facts import parse_company_facts
from runner_web.sec_training import SPLIT_FILES, _canonical_json
from runner_web.sec_training_v2 import (
    TARGET_FIELDS,
    _example,
    _fact_comparisons,
    _source,
    _target,
    semantic_chunks,
)

SOURCE_RELEASE_RE = re.compile(r"^braid_sec_[a-f0-9]{64}$")
ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
DEFAULT_MAX_BODY_BYTES = 128 * 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _body(line: dict[str, Any], binding: dict[str, Any], maximum_bytes: int) -> bytes:
    encoded = line.get("bodyBase64")
    if not isinstance(encoded, str):
        raise ValueError("bodyBase64 must be a string")
    try:
        body = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("bodyBase64 is not valid base64") from error
    if len(body) > maximum_bytes:
        raise ValueError(f"source body exceeds the {maximum_bytes}-byte limit")
    declared_bytes = binding.get("bytes")
    if not isinstance(declared_bytes, int) or declared_bytes != len(body):
        raise ValueError("source body size does not match its Braid binding")
    declared_digest = binding.get("sha256")
    if not isinstance(declared_digest, str) or not DIGEST_RE.fullmatch(declared_digest):
        raise ValueError("source body has an invalid Braid SHA-256 binding")
    if hashlib.sha256(body).hexdigest() != declared_digest:
        raise ValueError("source body does not match its Braid SHA-256 binding")
    return body


def _fact_record(fact: IssuerFact) -> dict[str, Any]:
    return {
        "concept": fact.concept,
        "value": float(fact.value),
        "unit": fact.unit,
        "period_start": fact.period_start.isoformat() if fact.period_start else None,
        "period_end": fact.period_end.isoformat(),
        "filed_at": fact.filed_at.astimezone(UTC).isoformat(),
        "accession": fact.accession,
        "form": fact.form,
        "source_tag": fact.source_tag,
    }


def _facts_as_of(
    facts: Iterable[IssuerFact], filed_at: datetime, maximum: int
) -> list[dict[str, Any]]:
    selected = [fact for fact in facts if fact.filed_at.astimezone(UTC) <= filed_at]
    selected.sort(key=lambda fact: (fact.concept, fact.accession))
    selected.sort(
        key=lambda fact: (
            fact.filed_at.astimezone(UTC),
            fact.period_end,
        ),
        reverse=True,
    )
    return [_fact_record(fact) for fact in selected[:maximum]]


def _classification_fields(
    filing: dict[str, Any], issuer: dict[str, Any], body: bytes
) -> dict[str, Any]:
    form = str(filing.get("form") or "").strip().upper()
    text = body.decode("utf-8", errors="replace")
    ownership: OwnershipSummary | None = None
    beneficial: BeneficialOwnershipSummary | None = None
    try:
        if form.startswith("4") and "ownershipDocument" in text:
            ownership = parse_ownership_xml(text)
        elif form.startswith(("SC 13D", "SC 13G")):
            beneficial = parse_beneficial_ownership_xml(text)
    except Exception:
        pass

    classification = classify_filing(form, ownership)
    tickers = issuer.get("tickers")
    ticker = str(tickers[0]).strip().upper() if isinstance(tickers, list) and tickers else ""
    if ownership and ownership.ticker:
        ticker = ownership.ticker
    is_purchase = bool(ownership and ownership.purchase_value)
    return {
        "accession": filing["accession"],
        "cik": int(filing["cik"]),
        "ticker": ticker,
        "company": str(filing["company"]),
        "form": form,
        "kind": classification["kind"],
        "sentiment": classification["sentiment"],
        "score": float(classification["score"]),
        "title": f"{form} filing for {filing['company']}",
        "filed_at": str(filing["filedAt"]),
        "filing_url": str(filing["filingUrl"]),
        "actor": ownership.owner_name if ownership else None,
        "actor_title": ownership.owner_title if ownership else None,
        "transaction_codes": ",".join(ownership.codes) if ownership else "",
        "transaction_shares": (ownership.purchase_shares if is_purchase else ownership.sale_shares)
        if ownership
        else None,
        "transaction_price": (
            ownership.average_purchase_price if is_purchase else ownership.average_sale_price
        )
        if ownership
        else None,
        "transaction_value": (ownership.purchase_value if is_purchase else ownership.sale_value)
        if ownership
        else None,
        "post_transaction_shares": ownership.post_transaction_shares if ownership else None,
        "stake_change_pct": ownership.stake_change_pct if ownership else None,
        "is_10b5_1": bool(ownership.is_10b5_1) if ownership else False,
        "direct_ownership": ownership.direct_ownership if ownership else None,
        "beneficial_ownership_pct": beneficial.ownership_pct if beneficial else None,
    }


def _braid_example(
    filing: dict[str, Any],
    *,
    task: str,
    suffix: str,
    evidence: dict[str, Any],
    answer: dict[str, Any],
    instruction: str,
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    example = _example(
        filing,
        task=task,
        suffix=suffix,
        evidence=evidence,
        answer=answer,
        instruction=instruction,
        sources=sources,
    )
    example["id"] = f"sec:{int(filing['cik'])}:{filing['accession']}:{task}:{suffix}"
    return example


def _examples_for_filing(
    filing: dict[str, Any],
    issuer: dict[str, Any],
    body: bytes,
    facts: Iterable[IssuerFact],
    *,
    max_document_chars: int,
    max_chunks: int,
    max_facts: int,
    max_examples: int,
) -> list[dict[str, Any]]:
    record = _classification_fields(filing, issuer, body)
    source = _object(filing.get("object"), "filing.object")
    text = body.decode("utf-8", errors="replace")[:max_document_chars]
    chunks = semantic_chunks(
        text,
        source_url=str(source["sourceUrl"]),
        source_sha256=str(source["sha256"]),
        content_type=str(source["contentType"]),
    )[:max_chunks]
    fact_records = _facts_as_of(facts, _utc(filing["filedAt"], "filing.filedAt"), max_facts)
    identity = {
        key: record[key]
        for key in ("accession", "cik", "ticker", "company", "form", "filed_at", "filing_url")
    }
    chunk_sources = [_source(chunk) for chunk in chunks]
    classification_evidence = {
        key: record.get(key)
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
        if record.get(key) is not None
    }
    examples = [
        _braid_example(
            record,
            task="sec_filing_structured_analysis",
            suffix="0",
            evidence={
                "filing": identity,
                "facts": fact_records,
                "document_chunks": [
                    {"source": _source(chunk), "text": chunk.text} for chunk in chunks[:1]
                ],
            },
            answer=_target(record),
            instruction=(
                "Return the requested structured filing fields: " + ", ".join(TARGET_FIELDS)
            ),
            sources=chunk_sources[:2],
        ),
        _braid_example(
            record,
            task="filing_classification",
            suffix="0",
            evidence={
                "filing": identity,
                "deterministic_parser_evidence": classification_evidence,
            },
            answer={
                "form": record["form"],
                "filing_kind": record["kind"],
                "sentiment": record["sentiment"],
                "score": float(record["score"]),
            },
            instruction="Classify this filing using only the supplied SEC evidence.",
            sources=[],
        ),
    ]
    for chunk in chunks:
        citation = _source(chunk)
        examples.append(
            _braid_example(
                record,
                task="evidence_navigation",
                suffix=str(chunk.index),
                evidence={"filing": identity, "document_chunk": chunk.text},
                answer={"accession": record["accession"], **citation},
                instruction="Identify and cite the exact supplied SEC evidence span.",
                sources=[citation],
            )
        )
    for index in range(0, min(len(fact_records), 32), 8):
        group = fact_records[index : index + 8]
        examples.append(
            _braid_example(
                record,
                task="xbrl_fact_extraction",
                suffix=str(index // 8),
                evidence={"filing": identity, "issuer_facts": group},
                answer={"accession": record["accession"], "facts": group},
                instruction="Return the supplied point-in-time XBRL facts without changing values.",
                sources=[],
            )
        )
    for index, comparison in enumerate(_fact_comparisons(fact_records)):
        comparison_facts = [
            fact
            for fact in fact_records
            if fact["concept"] == comparison["concept"]
            and fact["unit"] == comparison["unit"]
            and fact["period_end"]
            in {comparison["prior_period_end"], comparison["current_period_end"]}
        ][:2]
        examples.append(
            _braid_example(
                record,
                task="fact_comparison",
                suffix=str(index),
                evidence={"filing": identity, "facts": comparison_facts},
                answer=comparison,
                instruction="Compare the two latest distinct periods for the requested concept.",
                sources=[],
            )
        )
    known = {str(fact["concept"]) for fact in fact_records}
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
        inventory = sorted(
            {
                (str(fact["concept"]), str(fact["unit"]), str(fact["accession"]))
                for fact in fact_records
            }
        )
        examples.append(
            _braid_example(
                record,
                task="insufficient_evidence",
                suffix=missing,
                evidence={
                    "filing": identity,
                    "available_fact_inventory": [
                        {"concept": concept, "unit": unit, "accession": accession}
                        for concept, unit, accession in inventory
                    ],
                },
                answer={"concept": missing, "status": "insufficient_evidence", "value": None},
                instruction=f"Return the supported value for {missing}, or mark it insufficient.",
                sources=[],
            )
        )
    return examples[:max_examples]


def _split_plan(
    issuer_keys: set[str],
    timestamps_by_issuer: dict[str, set[str]],
    unseen_issuer_fraction: float,
) -> tuple[set[str], set[str], set[str]]:
    ordered = sorted(
        issuer_keys,
        key=lambda value: (hashlib.sha256(value.encode()).hexdigest(), value),
    )
    unseen_count = 0
    if len(ordered) >= 3 and unseen_issuer_fraction > 0:
        unseen_count = min(
            len(ordered) - 1,
            max(1, math.ceil(len(ordered) * unseen_issuer_fraction)),
        )
    unseen = set(ordered[:unseen_count])
    timestamps = sorted(
        {
            timestamp
            for issuer, values in timestamps_by_issuer.items()
            if issuer not in unseen
            for timestamp in values
        }
    )
    future: set[str] = set()
    validation: set[str] = set()
    if len(timestamps) >= 3:
        future_groups = max(1, math.ceil(len(timestamps) * 0.15))
        validation_groups = max(1, math.ceil(len(timestamps) * 0.10))
        overflow = future_groups + validation_groups - (len(timestamps) - 1)
        if overflow > 0:
            validation_groups = max(0, validation_groups - overflow)
        future = set(timestamps[-future_groups:])
        if validation_groups:
            validation = set(timestamps[-(future_groups + validation_groups) : -future_groups])
    elif len(timestamps) == 2:
        future = {timestamps[-1]}
    return unseen, validation, future


def _split_for(
    issuer_key: str,
    as_of: str,
    unseen: set[str],
    validation: set[str],
    future: set[str],
) -> str:
    if issuer_key in unseen:
        return "test_unseen_issuer"
    if as_of in future:
        return "test_future"
    if as_of in validation:
        return "validation"
    return "train"


def _training_text(example: dict[str, Any]) -> str:
    return "\n\n".join(
        f"{str(message['role']).capitalize()}:\n{message['content']}"
        for message in example["messages"]
    )


def _file_binding(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    return {
        "path": relative,
        "sha256": _sha256(path),
        "bytes": path.stat().st_size,
        "mediaType": "application/x-jsonlines"
        if relative.endswith(".jsonl")
        else "application/json",
    }


def _braid_source(
    root: Path,
    source_release_id: str,
    source_manifest_sha256: str,
    runner_revision: str,
    *,
    source_id: str,
    path: str,
    split: str,
) -> dict[str, Any]:
    candidate = root / path
    revision = f"{source_release_id}:manifest-{source_manifest_sha256}:runner-{runner_revision}"
    return {
        "id": source_id,
        "kind": "file",
        "path": path,
        "format": "jsonl",
        "textField": "text",
        "license": "NOASSERTION",
        "language": "en",
        "domain": "sec",
        "split": split,
        "maximumBytes": max(1, candidate.stat().st_size),
        "attribution": f"Braid SEC source release {source_release_id}",
        "snapshot": {"revision": revision, "sha256": _sha256(candidate)},
        "selectionRationale": "Deterministic FERAL-7B SEC multitask example.",
        "trainingObjective": "Ground SEC answers in accession-bound filing evidence.",
    }


def _braid_build(
    root: Path,
    source_release_id: str,
    source_manifest_sha256: str,
    runner_revision: str,
    *,
    name: str,
    description: str,
    purposes: tuple[str, ...],
    source_specs: tuple[tuple[str, str, str], ...],
) -> dict[str, Any]:
    digest = source_release_id.removeprefix("braid_sec_")
    sources = [
        _braid_source(
            root,
            source_release_id,
            source_manifest_sha256,
            runner_revision,
            source_id=source_id,
            path=path,
            split=split,
        )
        for source_id, path, split in source_specs
    ]
    split_minimums = {split: 1 for _source_id, _path, split in source_specs}
    minimum_documents = len(source_specs)
    return {
        "apiVersion": "braid/v1",
        "kind": "DatasetBuild",
        "metadata": {
            "name": name,
            "version": f"v1-{digest[:12]}",
            "description": description,
        },
        "spec": {
            "purposes": list(purposes),
            "sources": sources,
            "rights": {
                "allowedLicenses": ["NOASSERTION"],
                "requireAttribution": False,
                "requireEvidence": False,
                "requirePinnedSnapshots": True,
            },
            "chunking": {
                "targetCharacters": 1_000_000,
                "overlapCharacters": 0,
                "repeatDocumentHeader": False,
            },
            "quality": {
                "minimumCharacters": 1,
                "maximumCharacters": 1_000_000,
                "minimumAlphabeticRatio": 0,
                "minimumPrintableRatio": 0.98,
                "maximumUrlRatio": 1,
                "maximumRepeatedLineRatio": 1,
                "nearDuplicateHammingDistance": 0,
                "rejectEmailAddresses": False,
                "rejectIpAddresses": False,
                "rejectApiKeys": False,
                "forbiddenPatterns": [],
            },
            "selection": {
                "strategy": "ranked",
                "seed": "feral-7b-sec-v1",
                "qualityTemperature": 0.25,
                "domainWeights": {},
            },
            "evaluation": {
                "minimumDocuments": minimum_documents,
                "minimumSourceDocuments": minimum_documents,
                "minimumApproximateTokens": 1,
                "maximumRejectionRatio": 0,
                "requiredDomains": ["sec"],
                "minimumDocumentsByDomain": {"sec": minimum_documents},
                "minimumSourceDocumentsByDomain": {"sec": minimum_documents},
                "minimumDocumentsBySplit": split_minimums,
            },
            "output": {
                "directory": ".braid/releases",
                "includeRejectedText": False,
                "formats": ["jsonl"],
            },
            "publication": {"target": "none"},
            "budget": {"maximumGenerationRequests": 0, "maximumRuntimeSeconds": 3600},
        },
    }


def transform_braid_sec_stream(
    stream: TextIO,
    output_directory: Path,
    *,
    source_release_id: str,
    source_manifest_sha256: str,
    runner_revision: str,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_document_chars: int = 240_000,
    max_chunks_per_accession: int = 8,
    max_facts: int = 128,
    max_examples_per_accession: int = 20,
    max_filings_per_issuer: int = 40,
    unseen_issuer_fraction: float = 0.10,
    expected_issuers: int | None = None,
    expected_filings: int | None = None,
) -> dict[str, Any]:
    if not SOURCE_RELEASE_RE.fullmatch(source_release_id):
        raise ValueError("source_release_id must be a braid_sec_ release ID")
    if not DIGEST_RE.fullmatch(source_manifest_sha256):
        raise ValueError("source_manifest_sha256 must be a lowercase SHA-256 digest")
    if not re.fullmatch(r"[a-f0-9]{40}", runner_revision):
        raise ValueError("runner_revision must be a full lowercase Git commit SHA")
    if (
        min(
            max_body_bytes,
            max_document_chars,
            max_chunks_per_accession,
            max_examples_per_accession,
            max_filings_per_issuer,
        )
        < 1
        or max_facts < 0
    ):
        raise ValueError("body, document, chunk, fact, example, and filing limits are invalid")
    if not 0 <= unseen_issuer_fraction < 1:
        raise ValueError("unseen_issuer_fraction must be between 0 and 1")
    if expected_issuers is not None and expected_issuers < 1:
        raise ValueError("expected_issuers must be positive")
    if expected_filings is not None and expected_filings < 1:
        raise ValueError("expected_filings must be positive")
    if output_directory.exists() and (
        not output_directory.is_dir() or any(output_directory.iterdir())
    ):
        raise FileExistsError(f"output directory is not empty: {output_directory}")

    output_directory.parent.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(
            prefix=f".{output_directory.name}-", dir=output_directory.parent
        ) as temporary,
        closing(sqlite3.connect(Path(temporary) / "examples.sqlite3")) as database,
    ):
        root = Path(temporary)
        database.execute(
            "CREATE TABLE examples("
            "issuer_key TEXT,as_of TEXT,id TEXT PRIMARY KEY,task TEXT,payload TEXT)"
        )
        issuer_keys: set[str] = set()
        timestamps_by_issuer: dict[str, set[str]] = {}
        tasks: Counter[str] = Counter()
        characters = 0
        filings = 0
        examples = 0
        stream_issuers: set[int] = set()
        current_cik = 0
        current_facts: tuple[IssuerFact, ...] = ()
        current_filings = 0
        saw_filing = False

        for line_number, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                line = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ValueError(f"input line {line_number} is not valid JSON") from error
            line = _object(line, f"input line {line_number}")
            if line.get("schemaVersion") != "braid.sec-training-source/v1":
                raise ValueError(f"input line {line_number} has an unsupported schema")
            if line.get("releaseId") != source_release_id:
                raise ValueError(f"input line {line_number} is from a different Braid release")
            issuer = _object(line.get("issuer"), f"input line {line_number}.issuer")
            try:
                cik = int(issuer["cik"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"input line {line_number} has an invalid issuer CIK") from error
            if cik < current_cik:
                raise ValueError("Braid SEC stream issuers must be ordered by CIK")
            stream_issuers.add(cik)
            if cik != current_cik:
                current_cik = cik
                current_facts = ()
                current_filings = 0
                saw_filing = False
            kind = line.get("kind")
            if kind == "company-facts":
                if saw_filing:
                    raise ValueError("Braid company facts must precede filings for each issuer")
                source = _object(line.get("source"), f"input line {line_number}.source")
                if source.get("kind") != "company-facts":
                    raise ValueError("Braid company-facts line has the wrong source binding")
                body = _body(line, source, max_body_bytes)
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError as error:
                    raise ValueError("Braid company facts body is not valid JSON") from error
                if not isinstance(payload, dict) or int(payload.get("cik") or 0) != cik:
                    raise ValueError("Braid company facts body does not match its issuer")
                current_facts = parse_company_facts(
                    payload,
                    collected_at=_utc(source.get("collectedAt"), "source.collectedAt"),
                )
                continue
            if kind != "filing":
                raise ValueError(f"input line {line_number} has an unsupported source kind")
            filing = _object(line.get("filing"), f"input line {line_number}.filing")
            binding = _object(filing.get("object"), f"input line {line_number}.filing.object")
            if binding.get("kind") != "filing":
                raise ValueError("Braid filing line has the wrong source binding")
            try:
                filing_cik = int(filing["cik"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Braid filing has an invalid CIK") from error
            if filing_cik != cik:
                raise ValueError("Braid filing does not match its issuer")
            if str(filing.get("company") or "") != str(issuer.get("company") or ""):
                raise ValueError("Braid filing company does not match its issuer")
            if str(binding.get("sourceUrl") or "") != str(filing.get("filingUrl") or ""):
                raise ValueError("Braid filing URL does not match its source binding")
            accession = str(filing.get("accession") or "")
            if not ACCESSION_RE.fullmatch(accession):
                raise ValueError("Braid filing has an invalid accession")
            current_filings += 1
            if current_filings > max_filings_per_issuer:
                raise ValueError(
                    f"issuer {cik} exceeds the {max_filings_per_issuer}-filing transform limit"
                )
            saw_filing = True
            body = _body(line, binding, max_body_bytes)
            filing_examples = _examples_for_filing(
                filing,
                issuer,
                body,
                current_facts,
                max_document_chars=max_document_chars,
                max_chunks=max_chunks_per_accession,
                max_facts=max_facts,
                max_examples=max_examples_per_accession,
            )
            filings += 1
            for example in filing_examples:
                issuer_key = str(example["issuer_key"])
                as_of = str(example["as_of"])
                payload = _canonical_json(example)
                database.execute(
                    "INSERT INTO examples(issuer_key,as_of,id,task,payload) VALUES(?,?,?,?,?)",
                    (issuer_key, as_of, str(example["id"]), str(example["task"]), payload),
                )
                issuer_keys.add(issuer_key)
                timestamps_by_issuer.setdefault(issuer_key, set()).add(as_of)
                tasks[str(example["task"])] += 1
                characters += sum(len(str(message["content"])) for message in example["messages"])
                examples += 1
        database.commit()
        if filings == 0 or examples == 0:
            raise ValueError("Braid SEC stream contains no filing examples")
        if expected_issuers is not None and len(stream_issuers) != expected_issuers:
            raise ValueError(
                "Braid SEC stream contains "
                f"{len(stream_issuers)} issuers, expected {expected_issuers}"
            )
        if expected_filings is not None and filings != expected_filings:
            raise ValueError(
                f"Braid SEC stream contains {filings} filings, expected {expected_filings}"
            )

        unseen, validation, future = _split_plan(
            issuer_keys, timestamps_by_issuer, unseen_issuer_fraction
        )
        (root / "candidates").mkdir()
        handles = {
            "train": (root / "candidates/train.jsonl").open("w", encoding="utf-8", newline="\n"),
            "validation": (root / "candidates/validation.jsonl").open(
                "w", encoding="utf-8", newline="\n"
            ),
            "test_future": (root / "candidates/test-future.jsonl").open(
                "w", encoding="utf-8", newline="\n"
            ),
            "test_unseen_issuer": (root / "candidates/test-unseen-issuer.jsonl").open(
                "w", encoding="utf-8", newline="\n"
            ),
        }
        counts = {name: 0 for name in SPLIT_FILES}
        try:
            rows = database.execute(
                "SELECT issuer_key,as_of,payload FROM examples ORDER BY as_of,id"
            )
            for issuer_key, as_of, payload in rows:
                split = _split_for(str(issuer_key), str(as_of), unseen, validation, future)
                example = json.loads(str(payload))
                handles[split].write(
                    _canonical_json({"text": _training_text(example), **example}) + "\n"
                )
                counts[split] += 1
        finally:
            for handle in handles.values():
                handle.close()
            database.close()

        summary = {
            "schema": "stonks.sec_braid_transform_summary.v1",
            "source_release_id": source_release_id,
            "source_manifest_sha256": source_manifest_sha256,
            "runner_revision": runner_revision,
            "filings": filings,
            "examples": examples,
            "issuers": len(stream_issuers),
            "expected_counts": {
                "issuers": expected_issuers,
                "filings": expected_filings,
            },
            "estimated_training_tokens": characters // 4,
            "task_counts": dict(sorted(tasks.items())),
            "split_counts": counts,
            "split_policy": "accession-locked-issuer-hash-unseen-then-time-groups-v2",
            "target_policy": "deterministic-sec-multitask-no-teacher-no-market-outcomes-v2",
        }
        (root / "dataset-summary.json").write_text(
            _canonical_json(summary) + "\n", encoding="utf-8", newline="\n"
        )
        builds = {
            "feral-7b-training.braid.json": _braid_build(
                root,
                source_release_id,
                source_manifest_sha256,
                runner_revision,
                name="feral-7b-sec",
                description="FERAL-7B deterministic SEC multitask training corpus from Braid.",
                purposes=("fine-tuning", "evaluation", "research"),
                source_specs=(
                    ("feral-train", "candidates/train.jsonl", "train"),
                    ("feral-validation", "candidates/validation.jsonl", "validation"),
                ),
            ),
            "feral-7b-future-eval.braid.json": _braid_build(
                root,
                source_release_id,
                source_manifest_sha256,
                runner_revision,
                name="feral-7b-sec-future-eval",
                description="FERAL-7B sealed future SEC evaluation release from Braid.",
                purposes=("evaluation", "research"),
                source_specs=(("feral-future", "candidates/test-future.jsonl", "test"),),
            ),
            "feral-7b-unseen-eval.braid.json": _braid_build(
                root,
                source_release_id,
                source_manifest_sha256,
                runner_revision,
                name="feral-7b-sec-unseen-eval",
                description="FERAL-7B sealed unseen-issuer SEC evaluation release from Braid.",
                purposes=("evaluation", "research"),
                source_specs=(("feral-unseen", "candidates/test-unseen-issuer.jsonl", "test"),),
            ),
        }
        for filename, build in builds.items():
            (root / filename).write_text(
                json.dumps(build, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        artifact_paths = [
            "candidates/train.jsonl",
            "candidates/validation.jsonl",
            "candidates/test-future.jsonl",
            "candidates/test-unseen-issuer.jsonl",
            "dataset-summary.json",
            *builds,
        ]
        manifest = {
            "schema": "stonks.sec_braid_transform_release.v1",
            "source": {
                "release_id": source_release_id,
                "manifest_sha256": source_manifest_sha256,
            },
            "transform": {
                "repository": "https://github.com/atimics/runner-watch",
                "revision": runner_revision,
                "recipe": "src/runner_web/sec_braid_transform.py",
            },
            "artifacts": [_file_binding(root, path) for path in artifact_paths],
            "summary": summary,
            "training_authorized": False,
        }
        (root / "transform-release.json").write_text(
            _canonical_json(manifest) + "\n", encoding="utf-8", newline="\n"
        )
        (root / "examples.sqlite3").unlink()
        if output_directory.exists():
            output_directory.rmdir()
        shutil.move(str(root), output_directory)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transform a verified Braid SEC source stream into FERAL-7B corpus inputs"
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--source-release-id", required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--runner-revision", required=True)
    parser.add_argument("--max-body-bytes", type=int, default=DEFAULT_MAX_BODY_BYTES)
    parser.add_argument("--max-document-chars", type=int, default=240_000)
    parser.add_argument("--max-chunks-per-accession", type=int, default=8)
    parser.add_argument("--max-facts", type=int, default=128)
    parser.add_argument("--max-examples-per-accession", type=int, default=20)
    parser.add_argument("--max-filings-per-issuer", type=int, default=40)
    parser.add_argument("--unseen-issuer-fraction", type=float, default=0.10)
    parser.add_argument("--expected-issuers", type=int, required=True)
    parser.add_argument("--expected-filings", type=int, required=True)
    arguments = parser.parse_args()
    manifest = transform_braid_sec_stream(
        stream=sys.stdin,
        output_directory=arguments.output,
        source_release_id=arguments.source_release_id,
        source_manifest_sha256=arguments.source_manifest_sha256,
        runner_revision=arguments.runner_revision,
        max_body_bytes=arguments.max_body_bytes,
        max_document_chars=arguments.max_document_chars,
        max_chunks_per_accession=arguments.max_chunks_per_accession,
        max_facts=arguments.max_facts,
        max_examples_per_accession=arguments.max_examples_per_accession,
        max_filings_per_issuer=arguments.max_filings_per_issuer,
        unseen_issuer_fraction=arguments.unseen_issuer_fraction,
        expected_issuers=arguments.expected_issuers,
        expected_filings=arguments.expected_filings,
    )
    print(_canonical_json(manifest))


if __name__ == "__main__":
    main()
