from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_watch.ingestion import MarketEvent, SourceBatch, SourceFetch
from runner_web import db
from runner_web.db import connection, init_db
from runner_web.ingestion import record_source_batch
from runner_web.research_context import build_research_context, research_context_budget


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
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) "
            "VALUES(?,?,?,?,?)",
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
