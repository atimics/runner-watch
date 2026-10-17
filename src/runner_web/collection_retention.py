"""Small collection summaries and bounded retention for diagnostic item rows."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta

from runner_web.db import connection

DETAIL_DAYS = max(7, int(os.getenv("COLLECTION_DETAIL_DAYS", "30")))
DETAIL_RUNS = max(1000, int(os.getenv("COLLECTION_DETAIL_RUNS", "100000")))
SUMMARY_DAYS = max(DETAIL_DAYS, int(os.getenv("COLLECTION_SUMMARY_DAYS", "90")))
SUMMARY_RUNS = max(DETAIL_RUNS, int(os.getenv("COLLECTION_SUMMARY_RUNS", "500000")))
MAX_METADATA_BYTES = 16 * 1024
LOCK_ID = 731940129
REFERENCE_TABLES = (
    "security_quotes",
    "market_events",
    "issuer_facts",
    "entity_links",
    "macro_observations",
    "legal_case_candidates",
)


def compact_metadata(metadata: dict) -> dict:
    """Keep collection totals; stream cursors remain in their own current-state table."""
    result = dict(metadata)
    coverage = result.get("coverage")
    if isinstance(coverage, dict) and isinstance(coverage.get("streams"), list):
        coverage = dict(coverage)
        streams = coverage.pop("streams")
        encoded = json.dumps(streams, sort_keys=True, separators=(",", ":")).encode()
        coverage["stream_count"] = len(streams)
        coverage["streams_sha256"] = hashlib.sha256(encoded).hexdigest()
        result["coverage"] = coverage
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_METADATA_BYTES:
        result = {
            "metadata_summary": True,
            "original_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            **{
                k: result[k]
                for k in ("received_count", "partial", "interval")
                if isinstance(result.get(k), (int, float, bool))
                or isinstance(result.get(k), str)
                and len(result[k]) <= 100
            },
        }
    return result


def _cutoff(database, *, at: datetime, days: int, keep: int) -> str:
    cutoff = (at - timedelta(days=days)).isoformat()
    row = database.execute(
        "SELECT started_at FROM ingestion_runs ORDER BY started_at DESC,id DESC LIMIT 1 OFFSET ?",
        (keep - 1,),
    ).fetchone()
    return max(cutoff, row["started_at"]) if row else cutoff


def prune_collection_history(*, at: datetime | None = None, limit: int = 500) -> dict:
    """Commit one bounded batch. Keep provenance headers used by saved research."""
    current = at or datetime.now(UTC)
    result = {"detail_runs": 0, "items": 0, "summaries": 0}
    with connection() as database:
        if database.backend == "postgres":
            database.execute("SET LOCAL statement_timeout='15s'")
            database.execute("SET LOCAL lock_timeout='1s'")
            if not database.execute("SELECT pg_try_advisory_xact_lock(?)", (LOCK_ID,)).fetchone()[
                0
            ]:
                return result
        cutoff = _cutoff(database, at=current, days=DETAIL_DAYS, keep=DETAIL_RUNS)
        rows = database.execute(
            "SELECT id FROM ingestion_runs WHERE started_at<? AND details_pruned_at IS NULL "
            "AND finished_at IS NOT NULL AND status<>'recording' ORDER BY started_at,id LIMIT ?",
            (cutoff, max(1, min(limit, 1000))),
        ).fetchall()
        for row in rows:
            deleted = database.execute("DELETE FROM ingestion_items WHERE run_id=?", (row["id"],))
            result["items"] += max(0, deleted.rowcount)
            database.execute(
                "UPDATE ingestion_runs SET metadata_json='{}',details_pruned_at=? WHERE id=?",
                (current.isoformat(), row["id"]),
            )
        result["detail_runs"] = len(rows)
        cutoff = _cutoff(database, at=current, days=SUMMARY_DAYS, keep=SUMMARY_RUNS)
        # These headers tie normalized facts and coin observations to their source run.
        references = " UNION ".join(
            f"SELECT {column} AS id FROM {table}"
            for table in REFERENCE_TABLES
            for column in ("first_run_id", "last_run_id")
        )
        references += (
            " UNION SELECT run_id AS id FROM memecoin_assets"
            " UNION SELECT run_id AS id FROM memecoin_quote_history"
            " UNION SELECT r.id FROM ingestion_runs r JOIN ("
            "SELECT source,feed,MAX(finished_at) AS latest FROM ingestion_runs GROUP BY source,feed"
            ") f ON r.source=f.source AND r.feed=f.feed AND r.finished_at=f.latest"
        )
        # Skip the reference scan until at least one summary has reached its cutoff.
        if database.execute(
            "SELECT 1 FROM ingestion_runs WHERE started_at<? AND details_pruned_at IS NOT NULL "
            "LIMIT 1",
            (cutoff,),
        ).fetchone():
            deleted = database.execute(
                "WITH protected AS (" + references + ") "
                "DELETE FROM ingestion_runs WHERE id IN (SELECT r.id FROM ingestion_runs r "
                "WHERE r.started_at<? AND r.details_pruned_at IS NOT NULL "
                "AND NOT EXISTS(SELECT 1 FROM protected p WHERE p.id=r.id) "
                "ORDER BY r.started_at,r.id LIMIT ?) RETURNING id",
                (cutoff, max(1, min(limit, 1000))),
            )
            result["summaries"] = len(deleted.fetchall())
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES('collection_last_prune',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (json.dumps(result), current.isoformat()),
        )
    return result
