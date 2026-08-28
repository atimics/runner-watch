from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.cases import (
    case_revisions,
    create_case,
    get_case,
    infer_horizon_minutes,
    list_cases,
    update_case,
)
from runner_web.db import connection, init_db
from runner_web.pseudonyms import ensure_comment_avatar


def seed_user(user_id: str = "case-user") -> None:
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            (user_id, user_id, "Case User", "active", datetime.now(UTC).isoformat()),
        )


def test_comment_text_infers_a_horizon_without_an_extra_form() -> None:
    assert infer_horizon_minutes("Watching this today") == 390
    assert infer_horizon_minutes("Could work over 48h") == 2880
    assert infer_horizon_minutes("My view for the next month") == 43_200
    assert infer_horizon_minutes("No time written here") == 7200


def test_case_keeps_an_append_only_revision_history(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "cases.db")
    init_db()
    seed_user()

    created = create_case(
        "case-user",
        "ONE",
        thesis="A clean catalyst can support a move above the prior high.",
        horizon_minutes=1440,
        reference_price=1.25,
        invalidation="A close below VWAP invalidates the setup.",
        risks=["Dilution", "Dilution", "Low cash runway"],
        open_questions=["Is the new contract material?"],
        confidence=0.62,
    )
    updated = update_case(
        "case-user",
        created["public_id"],
        {
            "thesis": "The catalyst is confirmed, but price still needs to hold above VWAP.",
            "confidence": 0.71,
        },
        change_note="Primary filing confirmed the customer.",
    )

    assert updated is not None
    assert updated["confidence"] == 0.71
    assert updated["risks"] == ["Dilution", "Low cash runway"]
    revisions = case_revisions("case-user", created["public_id"])
    assert revisions is not None
    assert [row["revision_no"] for row in revisions] == [2, 1]
    assert revisions[0]["change_note"] == "Primary filing confirmed the customer."
    assert revisions[1]["thesis"] == created["thesis"]


def test_cases_are_private_to_their_owner(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "case-privacy.db")
    init_db()
    seed_user("owner")
    seed_user("other")
    created = create_case(
        "owner",
        "ONE",
        thesis="This thesis belongs only to its owner.",
        horizon_minutes=390,
        reference_price=None,
        invalidation="The named catalyst does not happen.",
        risks=[],
        open_questions=[],
        confidence=0.5,
    )

    assert len(list_cases("owner")) == 1
    assert list_cases("other") == []
    assert get_case("other", created["public_id"]) is None
    assert update_case(
        "other",
        created["public_id"],
        {"confidence": 0.9},
        change_note="Should not work",
    ) is None


def test_case_keeps_its_social_comment_source(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "case-source.db")
    init_db()
    seed_user()
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        avatar = ensure_comment_avatar(database, "case-user")
        database.execute(
            """
            INSERT INTO ticker_comments(id,ticker,user_id,body,status,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            ("source-comment", "ONE", "case-user", "Watching volume", "public", timestamp),
        )

    created = create_case(
        "case-user",
        "ONE",
        thesis="Watching volume",
        horizon_minutes=7200,
        reference_price=1.2,
        invalidation="Unknown — not supplied by the user.",
        risks=[],
        open_questions=[],
        confidence=None,
        source_comment_id="source-comment",
        source_kind="community_comment",
    )

    assert created["source_kind"] == "community_comment"
    assert created["source_comment_id"] == "source-comment"
    assert created["source_pseudonym"] == avatar["name"]
    assert created["confidence"] is None


def test_closing_a_case_preserves_the_final_outcome(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "case-outcome.db")
    init_db()
    seed_user()
    created = create_case(
        "case-user",
        "ONE",
        thesis="A one-day setup with a recorded final outcome.",
        horizon_minutes=1440,
        reference_price=2.0,
        invalidation="Price closes below the reference level.",
        risks=[],
        open_questions=[],
        confidence=0.55,
    )

    closed = update_case(
        "case-user",
        created["public_id"],
        {"status": "closed", "final_outcome": "Target reached before invalidation."},
        change_note="Case closed after the selected horizon.",
    )

    assert closed is not None
    assert closed["status"] == "closed"
    assert closed["closed_at"] is not None
    assert closed["final_outcome"] == "Target reached before invalidation."
    assert list_cases("case-user") == []
    assert len(list_cases("case-user", include_recent_closed=True)) == 1
    assert len(list_cases("case-user", include_inactive=True)) == 1
