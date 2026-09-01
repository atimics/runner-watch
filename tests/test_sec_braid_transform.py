from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from pytest import raises

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.sec_braid_transform import transform_braid_sec_stream
from runner_web.sec_braid_universe import export_braid_issuer_universe

SOURCE_RELEASE_ID = f"braid_sec_{'a' * 64}"
SOURCE_MANIFEST_SHA256 = "b" * 64
RUNNER_REVISION = "c" * 40


def _binding(kind: str, url: str, body: bytes) -> dict[str, Any]:
    return {
        "kind": kind,
        "sourceUrl": url,
        "contentType": "application/json" if kind == "company-facts" else "text/html",
        "sha256": hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "objectKey": f"raw/{hashlib.sha256(body).hexdigest()}",
        "collectedAt": "2026-08-31T00:00:00Z",
    }


def _line(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _stream() -> str:
    lines: list[str] = []
    for index, month in enumerate((2, 5, 8, 11), 1):
        cik = 1000 + index
        issuer = {"cik": cik, "company": f"Example {index}", "tickers": [f"EX{index}"]}
        accession = f"{cik:010d}-24-{index:06d}"
        if index == 1:
            facts = {
                "cik": cik,
                "facts": {
                    "us-gaap": {
                        "CashAndCashEquivalentsAtCarryingValue": {
                            "units": {
                                "USD": [
                                    {
                                        "end": "2023-12-31",
                                        "filed": "2024-01-15",
                                        "form": "10-K",
                                        "accn": accession,
                                        "val": 1250000,
                                    }
                                ]
                            }
                        }
                    }
                },
            }
            facts_body = json.dumps(facts, sort_keys=True).encode()
            facts_binding = _binding(
                "company-facts",
                f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                facts_body,
            )
            lines.append(
                _line(
                    {
                        "schemaVersion": "braid.sec-training-source/v1",
                        "releaseId": SOURCE_RELEASE_ID,
                        "kind": "company-facts",
                        "issuer": issuer,
                        "source": facts_binding,
                        "bodyBase64": base64.b64encode(facts_body).decode(),
                    }
                )
            )
        filing_body = (
            f"<html><h1>Item 1. Business</h1><p>Accession {accession} contains "
            f"deterministic filing evidence for Example {index}.</p></html>"
        ).encode()
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accession.replace('-', '')}/annual.htm"
        )
        binding = _binding("filing", url, filing_body)
        lines.append(
            _line(
                {
                    "schemaVersion": "braid.sec-training-source/v1",
                    "releaseId": SOURCE_RELEASE_ID,
                    "kind": "filing",
                    "issuer": issuer,
                    "filing": {
                        "accession": accession,
                        "cik": cik,
                        "company": issuer["company"],
                        "tickers": issuer["tickers"],
                        "form": "10-K",
                        "filedAt": f"2024-{month:02d}-01T12:00:00Z",
                        "filingDate": f"2024-{month:02d}-01",
                        "primaryDocument": "annual.htm",
                        "filingUrl": url,
                        "object": binding,
                    },
                    "bodyBase64": base64.b64encode(filing_body).decode(),
                }
            )
        )
    return "\n".join(lines) + "\n"


def _transform(stream: str, output: Path) -> dict[str, Any]:
    return transform_braid_sec_stream(
        io.StringIO(stream),
        output,
        source_release_id=SOURCE_RELEASE_ID,
        source_manifest_sha256=SOURCE_MANIFEST_SHA256,
        runner_revision=RUNNER_REVISION,
    )


def test_braid_stream_transform_builds_pinned_training_and_evaluation_inputs(
    tmp_path: Path,
) -> None:
    output = tmp_path / "feral"
    manifest = _transform(_stream(), output)

    assert manifest["training_authorized"] is False
    assert manifest["source"] == {
        "release_id": SOURCE_RELEASE_ID,
        "manifest_sha256": SOURCE_MANIFEST_SHA256,
    }
    summary = manifest["summary"]
    assert summary["filings"] == 4
    assert summary["issuers"] == 4
    assert all(summary["split_counts"][split] > 0 for split in summary["split_counts"])
    assert not (output / "examples.sqlite3").exists()

    train = json.loads((output / "candidates/train.jsonl").read_text().splitlines()[0])
    assert train["schema"] == "stonks.sec_chat_example.v2"
    assert train["text"].startswith("System:\n")
    assert [message["role"] for message in train["messages"]] == [
        "system",
        "user",
        "assistant",
    ]
    future = json.loads((output / "candidates/test-future.jsonl").read_text().splitlines()[0])
    assert future["schema"] == "stonks.sec_chat_example.v2"
    assert future["text"].startswith("System:\n")

    build = json.loads((output / "feral-7b-training.braid.json").read_text())
    assert build["metadata"]["name"] == "feral-7b-sec"
    assert build["spec"]["publication"] == {"target": "none"}
    assert build["spec"]["quality"]["maximumUrlRatio"] == 1
    for source in build["spec"]["sources"]:
        path = output / source["path"]
        assert source["snapshot"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    future_build = json.loads((output / "feral-7b-future-eval.braid.json").read_text())
    assert future_build["metadata"]["name"] == "feral-7b-sec-future-eval"
    assert future_build["spec"]["sources"][0]["split"] == "test"
    assert future_build["spec"]["purposes"] == ["evaluation", "research"]


def test_braid_stream_transform_is_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _transform(_stream(), first)
    _transform(_stream(), second)

    first_files = sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
    second_files = sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file())
    assert first_files == second_files
    assert all((first / path).read_bytes() == (second / path).read_bytes() for path in first_files)


def test_braid_stream_transform_names_shared_accessions_by_issuer(tmp_path: Path) -> None:
    lines = _stream().splitlines()
    filing_indexes = [
        index for index, line in enumerate(lines) if json.loads(line).get("kind") == "filing"
    ]
    first = json.loads(lines[filing_indexes[0]])
    second = json.loads(lines[filing_indexes[1]])
    accession = first["filing"]["accession"]
    second["filing"]["accession"] = accession
    lines[filing_indexes[1]] = _line(second)

    output = tmp_path / "shared-accession"
    _transform("\n".join(lines) + "\n", output)

    examples = [
        json.loads(line)
        for path in sorted((output / "candidates").glob("*.jsonl"))
        for line in path.read_text().splitlines()
    ]
    identifiers = [str(example["id"]) for example in examples]
    shared = sorted(
        identifier
        for identifier in identifiers
        if accession in identifier and ":sec_filing_structured_analysis:0" in identifier
    )

    assert shared == [
        f"sec:1001:{accession}:sec_filing_structured_analysis:0",
        f"sec:1002:{accession}:sec_filing_structured_analysis:0",
    ]
    assert len(identifiers) == len(set(identifiers))


def test_braid_stream_transform_rejects_a_body_hash_mismatch(tmp_path: Path) -> None:
    lines = _stream().splitlines()
    filing = json.loads(lines[-1])
    filing["filing"]["object"]["sha256"] = "d" * 64
    lines[-1] = _line(filing)

    with raises(ValueError, match="SHA-256"):
        _transform("\n".join(lines) + "\n", tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_braid_universe_export_uses_runner_scan_intersection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "runner.db")
    init_db()
    with connection() as database:
        database.executemany(
            """
            INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at)
            VALUES(?,?,?,?,?)
            """,
            [
                (1001, "EXM", "Example Corp", "Nasdaq", "2026-08-31"),
                (1001, "EXM.A", "Example Corp", "Nasdaq", "2026-08-31"),
                (1002, "OUT", "Outside Corp", "NYSE", "2026-08-31"),
            ],
        )
        database.executemany(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,breakout_pct,dollar_volume,quote_time,signals_json,
                risks_json,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    "scan-1",
                    "EXM",
                    1,
                    "watch",
                    "regular",
                    1,
                    0,
                    0,
                    0,
                    0,
                    1,
                    "2026-08-31T12:00:00Z",
                    "[]",
                    "[]",
                    "2026-08-31T12:00:00Z",
                ),
                (
                    "scan-2",
                    "EXM.A",
                    1,
                    "watch",
                    "regular",
                    1,
                    0,
                    0,
                    0,
                    0,
                    1,
                    "2026-08-31T12:00:00Z",
                    "[]",
                    "[]",
                    "2026-08-31T12:00:00Z",
                ),
            ],
        )

    output = tmp_path / "issuers.json"
    universe = export_braid_issuer_universe(output)
    assert universe == {
        "schemaVersion": "braid.sec-issuer-universe/v1",
        "issuers": [
            {"cik": 1001, "company": "Example Corp", "tickers": ["EXM", "EXM.A"]}
        ],
    }
    assert json.loads(output.read_text()) == universe
