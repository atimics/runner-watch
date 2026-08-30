from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from runner_watch.ingestion import (
    EntityLink,
    IssuerFact,
    MacroObservation,
    MarketEvent,
    SecurityQuote,
    SourceBatch,
    SourceFetch,
)
from runner_watch.source_catalog import SourcePolicy
from runner_web import db
from runner_web.db import connection, init_db
from runner_web.ingestion import ingestion_status, record_source_batch, register_source


def _fetch(started_at: datetime, finished_at: datetime) -> SourceFetch:
    return SourceFetch(
        source="example",
        feed="mixed",
        locator="https://example.test/feed.json",
        status="success",
        started_at=started_at,
        finished_at=finished_at,
        payload=b'{"ok":true}',
        content_type="application/json",
        metadata={"requested_count": 5, "received_count": 5},
    )


def _batch(fetch: SourceFetch, *, event_status: str = "active") -> SourceBatch:
    observed_at = datetime(2026, 8, 24, 20, tzinfo=UTC)
    return SourceBatch(
        fetch=fetch,
        market_events=(
            MarketEvent(
                event_id="evt-1",
                ticker="pen",
                event_type="test_event",
                event_at=observed_at,
                status=event_status,
                source_url=fetch.locator,
                published_at=observed_at - timedelta(seconds=10),
                payload={"status": event_status},
            ),
        ),
        security_quotes=(
            SecurityQuote(
                ticker="pen",
                observed_at=observed_at,
                bid=1.01,
                ask=1.03,
                bid_size=10,
                ask_size=12,
                last_trade=1.02,
                exchange="X",
                conditions=("R",),
            ),
        ),
        issuer_facts=(
            IssuerFact(
                cik=1234,
                concept="CashAndCashEquivalentsAtCarryingValue",
                value=2_500_000,
                unit="USD",
                period_end=date(2026, 6, 30),
                filed_at=observed_at - timedelta(days=3),
                accession="0000001234-26-000001",
                form="10-Q",
                source_tag="us-gaap:CashAndCashEquivalentsAtCarryingValue",
            ),
        ),
        entity_links=(
            EntityLink(
                external_id="sponsor-1",
                cik=1234,
                ticker="pen",
                confidence=0.98,
                method="reviewed_alias",
            ),
        ),
        macro_observations=(
            MacroObservation(
                series_id="TEST",
                observation_date=date(2026, 8, 22),
                vintage_date=date(2026, 8, 24),
                value=12.5,
            ),
        ),
    )


def test_source_batch_records_raw_and_normalized_rows_atomically(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "source-layer.db")
    init_db()
    started_at = datetime(2026, 8, 24, 20, tzinfo=UTC)
    first_fetch = _fetch(started_at, started_at + timedelta(seconds=1))
    first_run = record_source_batch(_batch(first_fetch))
    second_fetch = replace(first_fetch, finished_at=started_at + timedelta(seconds=61))
    second_run = record_source_batch(_batch(second_fetch, event_status="resolved"))

    with connection() as database:
        counts = {
            table: database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "ingestion_runs",
                "source_documents",
                "security_quotes",
                "market_events",
                "issuer_facts",
                "entity_links",
                "macro_observations",
            )
        }
        quote = database.execute(
            "SELECT ticker,first_run_id,last_run_id FROM security_quotes"
        ).fetchone()
        policy = database.execute(
            "SELECT review_status,display_policy FROM source_registry "
            "WHERE source='example' AND feed='mixed'"
        ).fetchone()
    assert counts == {
        "ingestion_runs": 2,
        "source_documents": 1,
        "security_quotes": 1,
        "market_events": 2,
        "issuer_facts": 1,
        "entity_links": 1,
        "macro_observations": 1,
    }
    assert tuple(quote) == ("PEN", first_run, second_run)
    assert tuple(policy) == ("review_required", "review_required")


def test_bad_normalized_record_rolls_back_projection_but_keeps_failed_run(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "source-error.db")
    init_db()
    timestamp = datetime(2026, 8, 24, 20, tzinfo=UTC)
    batch = SourceBatch(
        fetch=_fetch(timestamp, timestamp + timedelta(seconds=1)),
        entity_links=(
            EntityLink(
                external_id="bad",
                ticker="PEN",
                confidence=1.5,
                method="bad_test",
            ),
        ),
    )
    with pytest.raises(ValueError, match="confidence"):
        record_source_batch(batch)

    with connection() as database:
        run = database.execute("SELECT status,error FROM ingestion_runs").fetchone()
        documents = database.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
        links = database.execute("SELECT COUNT(*) FROM entity_links").fetchone()[0]
    assert run["status"] == "error"
    assert "Projection failed" in run["error"]
    assert documents == 0
    assert links == 0


def test_source_status_uses_registered_freshness_rules(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "source-status.db")
    init_db()
    register_source(
        SourcePolicy(
            source="example",
            feed="heartbeat",
            title="Example heartbeat",
            owner="Example",
            terms_url="https://example.test/terms",
            credential_env=None,
            expected_cadence_seconds=30,
            stale_after_seconds=60,
            schedule="always",
            storage_policy="normalized_only",
            display_policy="internal_only",
            attribution="Example",
        )
    )
    finished_at = datetime(2026, 8, 24, 20, tzinfo=UTC)
    record_source_batch(
        SourceBatch(
            fetch=SourceFetch(
                source="example",
                feed="heartbeat",
                locator="https://example.test/heartbeat",
                status="success",
                started_at=finished_at - timedelta(seconds=1),
                finished_at=finished_at,
                payload={"ok": True},
                content_type="application/json",
            )
        )
    )

    status = ingestion_status(as_of=finished_at + timedelta(seconds=61))
    source = next(
        row
        for row in status["sources"]
        if row["source"] == "example" and row["feed"] == "heartbeat"
    )
    assert source["health"] == "stale"
    assert source["active_now"] is True
    assert source["age_seconds"] == 61
    assert source["stale_at"] == (finished_at + timedelta(seconds=60)).isoformat()


def test_source_status_uses_normalized_company_map_freshness(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "company-map-status.db")
    init_db()
    refreshed_at = datetime(2026, 8, 24, 20, tzinfo=UTC)
    with connection() as database:
        database.execute(
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) VALUES(?,?,?,?,?)",
            (1, "MAP", "Mapped Company", "NASDAQ", refreshed_at.isoformat()),
        )

    status = ingestion_status(as_of=refreshed_at + timedelta(hours=1))
    source = next(
        row
        for row in status["sources"]
        if row["source"] == "sec" and row["feed"] == "company_map"
    )

    assert source["health"] == "healthy"
    assert source["normalized_items"] == 1
    assert source["last_success_at"] == refreshed_at.isoformat()


def test_source_status_treats_unused_event_feeds_as_idle(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "event-status.db")
    init_db()

    status = ingestion_status(as_of=datetime(2026, 8, 24, 20, tzinfo=UTC))
    source = next(
        row
        for row in status["sources"]
        if row["source"] == "sec" and row["feed"] == "document"
    )

    assert source["health"] == "idle"
    assert source["active_now"] is False
