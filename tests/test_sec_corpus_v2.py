from __future__ import annotations

import gzip
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch, raises

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.sec_backfill import (
    SUBMISSIONS_BASE,
    SUBMISSIONS_URL,
    SecHttpClient,
    backfill_sec_corpus,
    parse_submission_filings,
)
from runner_web.sec_training import SPLIT_FILES
from runner_web.sec_training_v2 import export_sec_training_corpus_v2, semantic_chunks


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True).encode()


def _submissions() -> dict[str, Any]:
    return {
        "cik": "1001",
        "name": "Example Corp",
        "filings": {
            "recent": {
                "accessionNumber": ["0000001001-24-000002", "0000001001-24-000003"],
                "filingDate": ["2024-05-01", "2024-05-02"],
                "acceptanceDateTime": [
                    "2024-05-01T14:30:00Z",
                    "2024-05-02T14:30:00Z",
                ],
                "form": ["10-K", "DEF 14A"],
                "primaryDocument": ["annual.htm", "proxy.htm"],
            },
            "files": [
                {
                    "name": "CIK0000001001-submissions-001.json",
                    "filingFrom": "2022-01-01",
                    "filingTo": "2023-12-31",
                },
                {
                    "name": "CIK0000001001-submissions-002.json",
                    "filingFrom": "2018-01-01",
                    "filingTo": "2021-12-31",
                },
            ],
        },
    }


def _history() -> dict[str, Any]:
    return {
        "accessionNumber": ["0000001001-23-000001"],
        "filingDate": ["2023-07-01"],
        "acceptanceDateTime": ["2023-07-01T12:00:00Z"],
        "form": ["8-K"],
        "primaryDocument": ["current.htm"],
    }


def test_parse_submission_filings_filters_dates_forms_and_unsafe_documents() -> None:
    payload = _submissions()
    payload["filings"]["recent"]["accessionNumber"].append("bad-primary")
    payload["filings"]["recent"]["filingDate"].append("2024-06-01")
    payload["filings"]["recent"]["acceptanceDateTime"].append("")
    payload["filings"]["recent"]["form"].append("8-K")
    payload["filings"]["recent"]["primaryDocument"].append("../bad.htm")
    filings = parse_submission_filings(
        [payload, _history()],
        cik=1001,
        company="Example Corp",
        ticker="EXM",
        start_date=date(2023, 1, 1),
        end_date=date(2024, 12, 31),
    )
    assert [filing.form for filing in filings] == ["8-K", "10-K"]
    assert filings[0].filing_url.endswith("/000000100123000001/current.htm")
    assert filings[1].filed_at == "2024-05-01T14:30:00+00:00"


def test_backfill_archives_historical_filings_and_resumes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "backfill.db")
    init_db()
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at)
            VALUES(1001,'EXM','Example Corp','Nasdaq','2024-01-01')
            """
        )

    recent_url = SUBMISSIONS_URL.format(cik=1001)
    history_url = SUBMISSIONS_BASE + "CIK0000001001-submissions-001.json"
    annual_url = (
        "https://www.sec.gov/Archives/edgar/data/1001/000000100124000002/annual.htm"
    )
    current_url = (
        "https://www.sec.gov/Archives/edgar/data/1001/000000100123000001/current.htm"
    )
    payloads = {
        recent_url: _json_bytes(_submissions()),
        history_url: _json_bytes(_history()),
        annual_url: b"<html><h1>Item 1</h1><p>Annual evidence.</p></html>",
        current_url: b"<html><h1>Item 2</h1><p>Current evidence.</p></html>",
    }
    calls: list[str] = []

    def download(url: str, timeout: float) -> tuple[bytes, str | None]:
        assert timeout == 5
        calls.append(url)
        return payloads[url], "application/json" if url.endswith(".json") else "text/html"

    result = backfill_sec_corpus(
        start_date=date(2023, 1, 1),
        end_date=date(2024, 12, 31),
        download=download,
        ciks=(1001,),
        max_documents=1,
        include_company_facts=False,
        timeout=5,
    )
    assert result.issuers_completed == 0
    assert result.submission_files_fetched == 2
    assert result.filings_selected == 2
    assert result.filings_inserted == 1
    assert result.documents_fetched == 1
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM sec_filings").fetchone()[0] == 1
        assert database.execute(
            "SELECT COUNT(*) FROM source_documents WHERE source='sec'"
        ).fetchone()[0] == 3
        assert database.execute(
            "SELECT COUNT(*) FROM sec_filings WHERE market_score IS NOT NULL"
        ).fetchone()[0] == 0

    calls_before_resume = len(calls)
    resumed = backfill_sec_corpus(
        start_date=date(2023, 1, 1),
        end_date=date(2024, 12, 31),
        download=download,
        ciks=(1001,),
        max_documents=1,
        include_company_facts=False,
        timeout=5,
    )
    assert resumed.issuers_completed == 1
    assert resumed.submission_files_fetched == 0
    assert resumed.archived_responses_reused == 2
    assert resumed.filings_inserted == 1
    assert resumed.filings_skipped == 1
    assert calls[calls_before_resume:] == [annual_url]
    final = backfill_sec_corpus(
        start_date=date(2023, 1, 1),
        end_date=date(2024, 12, 31),
        download=download,
        ciks=(1001,),
        include_company_facts=False,
        timeout=5,
    )
    assert final.issuers_skipped == 1
    calls_before_new_scope = len(calls)
    new_scope = backfill_sec_corpus(
        start_date=date(2023, 1, 1),
        end_date=date(2024, 12, 31),
        download=download,
        ciks=(1001,),
        max_filings_per_issuer=1,
        include_company_facts=False,
        timeout=5,
    )
    assert new_scope.issuers_completed == 1
    assert new_scope.submission_files_fetched == 1
    assert new_scope.archived_responses_reused == 1
    assert calls[calls_before_new_scope:] == [recent_url]


def test_sec_client_requires_contact_and_rejects_non_sec_urls() -> None:
    with raises(ValueError, match="contact"):
        SecHttpClient("anonymous")
    client = SecHttpClient("RunnerWatch test@example.com")
    with raises(ValueError, match="non-SEC"):
        client("https://example.com/data.json", 1)


def _insert_filing(
    database: Any, *, accession: str, cik: int, ticker: str, filed_at: str
) -> None:
    filing_url = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/"
        f"{accession.replace('-', '')}/filing.htm"
    )
    database.execute(
        """
        INSERT INTO sec_filings(
            accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
            filing_url,transaction_codes,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            accession,
            cik,
            ticker,
            f"{ticker} Corp",
            "10-K",
            "Financial report",
            "neutral",
            48.0,
            f"10-K for {ticker}",
            filed_at,
            filing_url,
            "",
            filed_at,
            filed_at,
        ),
    )
    body = (
        "<html><h1>Item 1. Business</h1><p>"
        + ("Business evidence. " * 190)
        + "</p><h1>Item 1A. Risk Factors</h1><p>"
        + ("Risk evidence. " * 210)
        + "</p></html>"
    ).encode()
    digest = hashlib.sha256(body).hexdigest()
    database.execute(
        """
        INSERT INTO source_documents(
            source,source_url,content_hash,content_type,content_encoding,content,
            first_collected_at,last_collected_at
        ) VALUES('sec',?,?,?,?,?,?,?)
        """,
        (filing_url, digest, "text/html", "gzip", gzip.compress(body, mtime=0), filed_at, filed_at),
    )


def _read_splits(directory: Path) -> dict[str, list[dict[str, Any]]]:
    return {
        split: [json.loads(line) for line in (directory / filename).read_text().splitlines()]
        for split, filename in SPLIT_FILES.items()
    }


def test_v2_export_is_deterministic_multitask_and_accession_safe(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "v2.db")
    init_db()
    with connection() as database:
        database.execute(
            """
            INSERT INTO ingestion_runs(
                id,source,feed,locator,status,requested_count,received_count,
                metadata_json,started_at,finished_at
            ) VALUES('facts','sec','company_facts','fixture','success',8,8,'{}',
                     '2024-01-01','2026-01-01')
            """
        )
        for issuer in range(1, 5):
            for period in range(1, 4):
                _insert_filing(
                    database,
                    accession=f"000000{issuer:04d}-26-00000{period}",
                    cik=issuer,
                    ticker=f"T{issuer}",
                    filed_at=f"2026-0{period}-01T00:00:00+00:00",
                )
            for fact_index, (period_end, value) in enumerate(
                [("2024-12-31", 100.0), ("2025-12-31", 125.0)]
            ):
                fact_id = f"fact-{issuer}-{fact_index}"
                database.execute(
                    """
                    INSERT INTO issuer_facts(
                        id,source,feed,cik,concept,value,unit,period_end,filed_at,
                        accession,payload_json,first_run_id,last_run_id,
                        first_collected_at,last_collected_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        fact_id,
                        "sec",
                        "company_facts",
                        issuer,
                        "cash",
                        value,
                        "USD",
                        period_end,
                        period_end + "T00:00:00+00:00",
                        fact_id,
                        "{}",
                        "facts",
                        "facts",
                        period_end,
                        period_end,
                    ),
                )

    arguments = {
        "repository": "https://github.com/atimics/runner-watch",
        "revision": "c" * 40,
        "source_path": "exports/feral-7b-sec-v2",
        "unseen_issuer_fraction": 0.25,
    }
    first = export_sec_training_corpus_v2(tmp_path / "first", **arguments)
    second = export_sec_training_corpus_v2(tmp_path / "second", **arguments)
    assert first.manifest_sha256 == second.manifest_sha256
    for name in [*SPLIT_FILES.values(), "dataset-summary.json", "corpus-release.json"]:
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()

    splits = _read_splits(tmp_path / "first")
    all_examples = [example for rows in splits.values() for example in rows]
    assert {example["task"] for example in all_examples} == {
        "sec_filing_structured_analysis",
        "filing_classification",
        "evidence_navigation",
        "xbrl_fact_extraction",
        "fact_comparison",
        "insufficient_evidence",
    }
    accession_to_split: dict[str, str] = {}
    for split, examples in splits.items():
        for example in examples:
            assert example["schema"] == "stonks.sec_chat_example.v2"
            accession = example["source"]["accession"]
            assert accession_to_split.setdefault(accession, split) == split
    unseen_issuers = {row["issuer_key"] for row in splits["test_unseen_issuer"]}
    seen_issuers = {
        row["issuer_key"]
        for split, rows in splits.items()
        if split != "test_unseen_issuer"
        for row in rows
    }
    assert unseen_issuers.isdisjoint(seen_issuers)

    navigation = next(row for row in all_examples if row["task"] == "evidence_navigation")
    citation = json.loads(navigation["messages"][-1]["content"])
    assert len(citation["source_sha256"]) == 64
    assert len(citation["chunk_sha256"]) == 64
    assert citation["normalized_char_end"] > citation["normalized_char_start"]
    structured = next(
        row for row in all_examples if row["task"] == "sec_filing_structured_analysis"
    )
    structured_evidence = json.loads(structured["messages"][1]["content"].split("\nEVIDENCE\n")[1])
    assert len(structured_evidence["document_chunks"]) <= 1
    classification = next(
        row for row in all_examples if row["task"] == "filing_classification"
    )
    classification_evidence = json.loads(
        classification["messages"][1]["content"].split("\nEVIDENCE\n")[1]
    )
    assert "document_chunks" not in classification_evidence
    assert classification["source"]["evidence"] == []
    comparison = next(row for row in all_examples if row["task"] == "fact_comparison")
    comparison_evidence = json.loads(
        comparison["messages"][1]["content"].split("\nEVIDENCE\n")[1]
    )
    assert len(comparison_evidence["facts"]) == 2
    insufficient = next(row for row in all_examples if row["task"] == "insufficient_evidence")
    insufficient_evidence = json.loads(
        insufficient["messages"][1]["content"].split("\nEVIDENCE\n")[1]
    )
    assert "issuer_facts" not in insufficient_evidence
    assert insufficient_evidence["available_fact_inventory"]
    summary = json.loads((tmp_path / "first" / "dataset-summary.json").read_text())
    assert summary["filings"] == 12
    assert summary["examples"] > summary["filings"]
    assert summary["estimated_training_tokens"] > 0


def test_semantic_chunks_follow_sec_sections() -> None:
    body = (
        "<h1>Item 1. Business</h1><p>Business text.</p>"
        "<h1>Item 1A. Risk Factors</h1><p>Risk text.</p>"
    )
    chunks = semantic_chunks(
        body,
        source_url="https://www.sec.gov/Archives/example.htm",
        source_sha256="a" * 64,
        content_type="text/html",
        target_chars=256,
        max_chars=512,
    )
    assert [chunk.section for chunk in chunks] == ["Item 1. Business", "Item 1A. Risk Factors"]
    assert chunks[0].sha256 == hashlib.sha256(chunks[0].text.encode()).hexdigest()
