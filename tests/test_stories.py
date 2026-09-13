from __future__ import annotations

from pathlib import Path

import pytest
from pytest import MonkeyPatch

from runner_web import db as db_module
from runner_web.calls import create_call
from runner_web.db import connection
from runner_web.market_screens import listing
from runner_web.stories import (
    add_story_update,
    attach_call,
    open_story,
    public_story,
    resolve_story,
    stories_by_subject,
)


@pytest.fixture
def story_db(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db_module, "DATABASE_PATH", tmp_path / "stories.db")
    db_module.DATABASE_URL = ""
    db_module.REQUIRE_DATABASE_URL = False
    db_module.init_db()


def test_open_story_converges_on_one_versioned_story(story_db) -> None:
    first = open_story("stocks", "ticker", "SOUN", "Does SOUN hold its breakout?", at="a")
    second = open_story(
        "stocks",
        "ticker",
        "SOUN",
        "  does   SOUN hold  its breakout? ",
        at="b",
    )

    assert first["id"] == second["id"]
    with connection() as database:
        rows = database.execute("SELECT * FROM ticker_stories").fetchall()
    assert len(rows) == 1


def test_two_simultaneous_stories_share_one_ticker(story_db) -> None:
    breakout = open_story("stocks", "ticker", "SOUN", "Does SOUN hold its breakout?", at="a")
    dilution = open_story(
        "stocks", "ticker", "SOUN", "Does the new filing signal dilution?", at="a"
    )

    assert breakout["id"] != dilution["id"]
    grouped = stories_by_subject("stocks", ["SOUN"])
    assert len(grouped["SOUN"]) == 2


def test_updates_version_and_corrections_preserve_the_original(story_db) -> None:
    story = open_story(
        "stocks",
        "ticker",
        "SOUN",
        "Does SOUN hold its breakout?",
        revisit_trigger="next session close",
        next_review_at="2026-09-15T20:00:00+00:00",
        reference=("report", "report-one"),
        at="2026-09-12T15:00:00+00:00",
    )
    first_update = add_story_update(
        story["id"],
        "report",
        note="Volume confirms the move.",
        at="2026-09-12T16:00:00+00:00",
    )
    correction = add_story_update(
        story["id"],
        "correction",
        note="The earlier read misread the float.",
        correction_of_update_id=first_update["id"],
        at="2026-09-12T17:00:00+00:00",
    )

    assert first_update["version"] == 1
    assert correction["version"] == 2
    assert correction["correction_of_update_id"] == first_update["id"]
    with connection() as database:
        original = database.execute(
            "SELECT * FROM ticker_story_updates WHERE id=?", (first_update["id"],)
        ).fetchone()
    assert original["note"] == "Volume confirms the move."
    assert original["kind"] == "report"


def test_resolved_outcome_is_durable_and_a_new_update_reopens(story_db) -> None:
    story = open_story("stocks", "ticker", "SOUN", "Does SOUN hold its breakout?", at="a")
    resolve_story(story["id"], "The breakout held into the close.", at="b")
    resolved = public_story("stocks", "SOUN")

    assert resolved["status"] == "resolved"
    assert resolved["outcome"] == "The breakout held into the close."

    add_story_update(story["id"], "fact", note="It broke again overnight.", at="c")
    reopened = public_story("stocks", "SOUN")

    assert reopened["status"] == "open"
    assert reopened["outcome"] is None
    with connection() as database:
        updates = database.execute(
            "SELECT kind,note FROM ticker_story_updates WHERE story_id=? ORDER BY version",
            (story["id"],),
        ).fetchall()
    assert [str(row["kind"]) for row in updates] == ["close", "fact"]


def test_calls_link_to_the_story_and_keep_their_own_identity(story_db) -> None:
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) "
            "VALUES('alice','alice','Alice','active','2026-09-12T00:00:00+00:00')"
        )
    call = create_call("alice", "SOUN", entry_price=1.5, entry_at="2026-09-12T15:00:00+00:00")
    story = open_story("stocks", "ticker", "SOUN", "Does SOUN hold its breakout?", at="a")

    assert attach_call(story["id"], call["public_id"]) is True
    assert attach_call(story["id"], call["public_id"]) is False

    with connection() as database:
        stored = database.execute(
            "SELECT status,entry_price,ticker FROM community_calls WHERE public_id=?",
            (call["public_id"],),
        ).fetchone()
        links = database.execute(
            "SELECT link_kind,link_id FROM ticker_story_links WHERE story_id=?",
            (story["id"],),
        ).fetchall()
    assert stored["status"] == "active" and stored["entry_price"] == 1.5
    assert [(str(row["link_kind"]), str(row["link_id"])) for row in links] == [
        ("call", call["public_id"])
    ]


def test_public_story_carries_question_cue_and_outcome_only(story_db) -> None:
    story = open_story(
        "stocks",
        "ticker",
        "SOUN",
        "Does SOUN hold its breakout?",
        revisit_trigger="next session close",
        next_review_at="2026-09-15T20:00:00+00:00",
        reference=("report", "report-one"),
        at="2026-09-12T15:00:00+00:00",
    )
    resolve_story(story["id"], "Held.", at="b")
    payload = public_story("stocks", "SOUN")

    assert payload["question"] == "Does SOUN hold its breakout?"
    assert payload["status"] == "resolved"
    assert payload["outcome"] == "Held."
    assert payload["next_review_at"] == "2026-09-15T20:00:00+00:00"
    assert payload["revisit_trigger"] == "next session close"
    assert "id" not in payload and "story_key" not in payload
    assert "opening_reference_kind" not in payload and "opening_reference_id" not in payload


def test_listing_attaches_story_cues_to_matching_rows(story_db) -> None:
    open_story("stocks", "ticker", "SOUN", "Does SOUN hold its breakout?", at="a")
    rows = [
        {"ticker": "SOUN", "price": 8.4, "change_pct": 2.0, "score": 80, "company": "SOUN Co"},
        {"ticker": "AAPL", "price": 210.0, "change_pct": -1.0, "score": 40, "company": "Apple"},
    ]
    screen = listing("stocks", rows, stories=stories_by_subject("stocks", ["SOUN", "AAPL"]))

    assert screen["rows"][0]["story"]["question"] == "Does SOUN hold its breakout?"
    assert "story" not in screen["rows"][1]


def test_review_time_stays_distinct_from_call_settlement(story_db) -> None:
    story = open_story(
        "stocks",
        "ticker",
        "SOUN",
        "Does SOUN hold its breakout?",
        next_review_at="2026-09-15T20:00:00+00:00",
        at="a",
    )
    from runner_web.market_screens import settlement_terms

    assert story["next_review_at"] == "2026-09-15T20:00:00+00:00"
    assert "Settles after session close" in settlement_terms("stocks", "2026-09-12T15:00:00+00:00")
