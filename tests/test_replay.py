"""Replay honesty: what the board knew, and how the served ordering scores.

Two properties are pinned here. A replay at a past moment must not read evidence
from after it, and a training group whose outcome window had not closed must not
straddle the split. The third test scores the ordering the list actually shows.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from runner_watch.ingestion import MarketEvent, SourceBatch, SourceFetch
from runner_web import db, replay
from runner_web.db import connection, init_db
from runner_web.ingestion import record_source_batch
from tests.test_mobile import _approve_test_source, insert_scan_run, insert_scored_snapshot

NOW = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "replay.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    init_db()
    with db.connection() as connection_handle:
        yield connection_handle


def _seed_scan(captured: datetime) -> None:
    insert_scan_run("replay-run", captured.isoformat(), 1)
    insert_scored_snapshot("replay-snapshot", "replay-run", "ONE", 70, 1, captured.isoformat())


def _insert_filing(accession: str, collected: datetime, *, ticker: str = "ONE") -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_filings(
                accession,ticker,cik,form,filed_at,created_at,updated_at,filing_url,title,
                company,score,sentiment,kind,evidence_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                accession,
                ticker,
                1,
                "4",
                collected.isoformat(),
                collected.isoformat(),
                collected.isoformat(),
                "https://www.sec.gov/Archives/edgar/data/1/one.txt",
                "Statement of changes",
                "One Corp",
                60.0,
                "positive",
                "Insider purchase",
                "{}",
            ),
        )


def _insert_event(event_id: str, event_at: datetime) -> None:
    """Events reach the public view through an approved source, as in production."""

    fetch = SourceFetch.success(
        source="test_discovery",
        feed="mixed",
        locator="https://example.test/discovery",
        started_at=event_at,
        payload={},
        content_type="application/json",
    )
    record_source_batch(
        SourceBatch(
            fetch=replace(fetch, finished_at=event_at),
            market_events=(
                MarketEvent(
                    event_id=event_id,
                    ticker="ONE",
                    event_type="news_article",
                    event_at=event_at,
                    status="published",
                    source_url="https://example.test/event",
                    payload={},
                ),
            ),
        )
    )


def _insert_comment(comment_id: str, created: datetime) -> None:
    with connection() as database:
        # Comments hang off a user, so the reader has to exist first.
        database.execute(
            "INSERT OR IGNORE INTO users(id,username,display_name,status,created_at) "
            "VALUES('reader','reader','Reader','active',?)",
            (created.isoformat(),),
        )
        database.execute(
            "INSERT INTO ticker_comments(id,ticker,user_id,body,status,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (comment_id, "ONE", "reader", "watching", "public", created.isoformat()),
        )


def test_a_replay_reads_only_what_was_known_by_then(database):
    at = NOW
    _seed_scan(at - timedelta(hours=2))
    _insert_filing("known", at - timedelta(hours=1))
    _insert_filing("later", at + timedelta(minutes=30))
    _insert_event("known-event", at - timedelta(minutes=30))
    _insert_event("later-event", at + timedelta(minutes=5))
    # Approve after collecting: the ingestion path re-applies the source policy.
    _approve_test_source("test_discovery", "mixed")
    _insert_comment("known-comment", at - timedelta(minutes=10))
    _insert_comment("later-comment", at + timedelta(minutes=1))

    inputs = replay.point_in_time_inputs(at, ticker="ONE")

    # Only the filing that had been collected by then is a catalyst.
    assert inputs["filings_by_ticker"]["ONE"]["accession"] == "known"
    assert inputs["filing_counts"]["ONE"] == 1
    # Comments counted then, not now.
    assert inputs["community"]["ONE"]["comment_count"] == 1
    # Events bounded the same way.
    event_times = [event["event_at"] for event in inputs["market_events_by_ticker"].get("ONE", [])]
    assert event_times == [(at - timedelta(minutes=30)).isoformat()]
    # The three clocks are named, and none of them is later than the decision.
    assert inputs["feature_as_of"] == at.isoformat()
    assert inputs["computed_at"] == at.isoformat()
    assert inputs["score_as_of"] == at.isoformat()
    assert inputs["quote_as_of"] <= at.isoformat()


def test_a_replay_ignores_a_model_that_did_not_exist_yet(database):
    at = NOW
    _seed_scan(at - timedelta(hours=2))
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO ranker_models(
                id,feature_schema_version,horizon,model_kind,weights_json,metrics_json,
                training_start,training_end,training_groups,training_rows,status,created_at
            ) VALUES('late-model','stonks.ranker_features.v4','60m','test','{}','{}',?,?,1,1,
                     'active',?)
            """,
            (
                (at - timedelta(days=1)).isoformat(),
                (at - timedelta(hours=1)).isoformat(),
                (at + timedelta(hours=1)).isoformat(),
            ),
        )
        conn.execute(
            """
            INSERT INTO ranker_predictions(
                snapshot_id,model_id,score,rank,created_at,probability_up,probability_down,
                probability_timeout,expected_return_pct
            ) VALUES('replay-snapshot','late-model',80,1,?,0.8,0.1,0.1,4.0)
            """,
            ((at - timedelta(hours=1)).isoformat(),),
        )

    inputs = replay.point_in_time_inputs(at, ticker="ONE")

    assert inputs["predictions"] == {}


def _group(run_id: str, captured: datetime) -> list[dict[str, str]]:
    return [{"scan_run_id": run_id, "captured_at": captured.isoformat(), "ticker": "ONE"}]


def test_a_group_whose_outcome_window_is_open_at_the_split_is_purged():
    base = NOW
    # Five groups, one every ten minutes; the horizon is an hour, so the group
    # just before the boundary still has an open window when the next starts.
    groups = [_group(f"run-{index}", base + timedelta(minutes=10 * index)) for index in range(5)]

    result = replay.purge_boundary_groups(groups, split=0.8)

    # The 80% boundary is group 4, and every training window is still open there.
    assert result["purged"] == 4
    assert [group[0]["scan_run_id"] for group in result["groups"]] == ["run-4"]
    assert result["purged_runs"] == ["run-0", "run-1", "run-2", "run-3"]


def test_a_group_whose_window_closed_before_the_boundary_is_kept():
    base = NOW
    # Two hours apart, so every window has closed long before the held-out part.
    groups = [_group(f"run-{index}", base + timedelta(hours=2 * index)) for index in range(5)]

    result = replay.purge_boundary_groups(groups, split=0.8)

    assert result["purged"] == 0
    assert len(result["groups"]) == 5


def test_the_served_ordering_is_what_gets_scored():
    rows = [
        {"attention": 90, "model_payoff_bp": 10, "barrier_label": "up"},
        {"attention": 80, "model_payoff_bp": 800, "barrier_label": "timeout"},
        {"attention": 85, "model_payoff_bp": -400, "barrier_label": "down", "blocked": True},
        {"attention": 10, "model_payoff_bp": 400, "barrier_label": "timeout"},
    ]

    report = replay.evaluate_served_policy(rows, k=2)

    assert report["rows"] == 4
    assert report["k"] == 2
    assert report["base_rate"] == 0.5
    # Attention first surfaces the mover and the blocked adverse name: both are
    # material activity, which is what the list is for.
    assert report["served_precision"] == 1.0
    # The payoff ordering the reader never sees would have surfaced two timeouts.
    assert report["payoff_precision"] == 0.0
    assert report["blocked_in_top"] == 1
    assert report["blocked_share"] == 0.5


def test_evaluation_without_outcomes_says_so():
    assert replay.evaluate_served_policy([])["rows"] == 0
    unlabeled = replay.evaluate_served_policy([{"attention": 1, "barrier_label": None}])
    assert unlabeled["base_rate"] is None
