from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_watch.ingestion import MarketEvent, SourceBatch, SourceFetch
from runner_web import db
from runner_web.db import connection, init_db
from runner_web.ingestion import record_source_batch
from runner_web.research_context import (
    build_research_context,
    research_context_budget,
    research_evidence_metrics,
)


def test_context_budget_uses_configured_fill_ratio(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_RESEARCH_CONTEXT_TOKENS", "100000")
    monkeypatch.setenv("OPENROUTER_RESEARCH_CONTEXT_FILL_RATIO", "0.8")
    monkeypatch.setenv("OPENROUTER_RESEARCH_OUTPUT_RESERVE_TOKENS", "10000")

    budget = research_context_budget()

    assert budget["target_input_tokens"] == 80_000
    assert budget["fill_ratio"] == 0.8


def test_one_shot_context_includes_filings_people_and_social_reports(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "research-context.db")
    init_db()
    timestamp = datetime.now(UTC)
    filed_at = timestamp.isoformat()
    filing_url = "https://www.sec.gov/Archives/edgar/data/1/one-index.html"
    social_url = "https://social.example/posts/one"
    with connection() as database:
        database.execute(
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) VALUES(?,?,?,?,?)",
            (1, "ONE", "One Company", "Nasdaq", filed_at),
        )
        database.execute(
            """
            INSERT INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                filing_url,actor,actor_title,transaction_codes,transaction_shares,
                transaction_price,transaction_value,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "one-accession",
                1,
                "ONE",
                "One Company",
                "4",
                "Insider transaction",
                "neutral",
                40,
                "Form 4",
                filed_at,
                filing_url,
                "Alex Example",
                "Director",
                "P",
                20_000,
                1.25,
                25_000,
                filed_at,
                filed_at,
            ),
        )
    fetch = SourceFetch.success(
        source="test_social",
        feed="reports",
        locator=social_url,
        started_at=timestamp,
        payload=b'{"text":"Customers are discussing a delayed product launch."}',
        content_type="application/json",
    )
    record_source_batch(
        SourceBatch(
            fetch=fetch,
            market_events=(
                MarketEvent(
                    event_id="one-social",
                    ticker="ONE",
                    event_type="social_spike",
                    event_at=timestamp,
                    published_at=timestamp,
                    status="active",
                    source_url=social_url,
                    payload={"mention_count": 18, "summary": "Launch delay discussion"},
                ),
            ),
        )
    )
    with connection() as database:
        database.execute(
            """
            UPDATE source_registry
            SET review_status='approved',display_policy='source_link_with_attribution'
            WHERE source='test_social' AND feed='reports'
            """
        )
    primary = {
        "ticker": "ONE",
        "company": "One Company",
        "filings": [
            {
                "form": "4",
                "actor": "Alex Example",
                "actor_title": "Director",
                "url": filing_url,
            }
        ],
    }

    packet = build_research_context("ONE", primary, token_budget=12_000)
    kinds = {section["kind"] for section in packet["context_sections"]}

    assert "official_company_identity" in kinds
    assert "structured_sec_filing" in kinds
    assert "system_event:social_spike" in kinds
    assert "raw_source_document:test_social" in kinds
    assert social_url in packet["sources"]
    assert packet["context_stats"]["used_input_tokens_estimate"] <= 12_000


def test_evidence_metrics_count_duplicate_rows_as_one_source_family() -> None:
    context = {
        "evidence_as_of": "2026-09-03T12:00:00+00:00",
        "context_sections": [
            {"kind": "structured_sec_filing", "observed_at": "2026-09-03T10:00:00Z"},
            {"kind": "structured_sec_filing", "observed_at": "2026-09-03T11:00:00Z"},
            {"kind": "sec_company_fact", "observed_at": "2026-09-03T09:00:00Z"},
        ],
    }

    metrics = research_evidence_metrics(context)

    assert metrics["source_family_count"] == 1
    assert metrics["fresh_source_family_count"] == 1


def test_evidence_metrics_mark_a_family_stale_after_24_hours() -> None:
    context = {
        "evidence_as_of": "2026-09-03T12:00:00+00:00",
        "context_sections": [
            {"kind": "system_event:news_article", "observed_at": "2026-09-02T11:59:59Z"},
            {"kind": "stored_market_bars", "observed_at": "2026-09-02T12:00:00Z"},
        ],
    }

    metrics = research_evidence_metrics(context)

    assert metrics["source_family_count"] == 2
    assert metrics["fresh_source_family_count"] == 1
    assert metrics["freshness_window_hours"] == 24


def test_evidence_metrics_match_distinct_report_points_to_citations() -> None:
    context = {"evidence_as_of": "2026-09-03T12:00:00+00:00"}
    report = {
        "catalysts": ["Revenue grew", "Revenue grew"],
        "risks": ["Cash is low"],
        "watch": ["New filing", "Price held"],
        "citations": [
            {"claim": "Revenue grew", "source_urls": ["https://example.com/a"]},
            {"claim": "revenue   grew", "source_urls": ["https://example.com/a"]},
            {"claim": "Cash is low", "source_urls": []},
            {
                "claim": "Price held",
                "source_urls": [],
                "source_receipts": [{"receipt_id": "ev_market"}],
            },
            {"claim": "Different claim", "source_urls": ["https://example.com/b"]},
        ],
        "sources": [],
    }

    metrics = research_evidence_metrics(context, report)

    assert metrics["report_claim_count"] == 4
    assert metrics["linked_report_claim_count"] == 2


def test_evidence_metrics_count_each_public_link_once() -> None:
    context = {"evidence_as_of": "2026-09-03T12:00:00+00:00"}
    report = {
        "catalysts": [],
        "risks": [],
        "watch": [],
        "sources": ["https://example.com/a", "https://example.com/a"],
        "citations": [
            {
                "claim": "A claim",
                "source_urls": ["https://example.com/a", "https://example.com/b"],
            }
        ],
    }

    metrics = research_evidence_metrics(context, report)

    assert metrics["public_link_count"] == 2


def test_market_bars_get_stable_internal_receipts(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "market-receipts.db")
    init_db()
    observed_at = "2026-08-25T19:55:00+00:00"
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,open,high,low,close,volume,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "test_archive",
                "ONE",
                "5m",
                observed_at,
                1.0,
                1.3,
                0.9,
                1.25,
                5000,
                observed_at,
                observed_at,
            ),
        )
    primary = {"ticker": "ONE", "captured_at": observed_at, "price": 1.25}

    first = build_research_context("ONE", primary, as_of=observed_at)
    second = build_research_context("ONE", primary, as_of=observed_at)
    first_bars = next(
        item for item in first["context_sections"] if item["kind"] == "stored_market_bars"
    )
    second_bars = next(
        item for item in second["context_sections"] if item["kind"] == "stored_market_bars"
    )

    assert first_bars["evidence_id"] == second_bars["evidence_id"]
    assert first_bars["source_url"] is None
    assert first_bars["source_receipt"] == {
        "receipt_id": first_bars["evidence_id"],
        "source_type": "stored_market_bars",
        "ticker": "ONE",
        "observed_at": observed_at,
    }
    assert first["primary_evidence_receipt"]["receipt_id"] == first["primary_evidence_id"]
