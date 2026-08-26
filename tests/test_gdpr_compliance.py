from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch
import pytest

from runner_web import db
from runner_web.caller_ids import (
    AdditionalCallerIdPaymentRequired,
    claim_caller_id,
    delete_caller_id,
)
from runner_web.db import connection, init_db
from runner_web.privacy import delete_user_data, export_user_data, purge_passive_tracking
from runner_web.pseudonyms import ensure_scoped_alias


ROOT = Path(__file__).resolve().parents[1]


def _seed_user() -> None:
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("gdpr-user", "member_gdpr", "Member", "active", timestamp),
        )
        database.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            ("session-hash", "gdpr-user", timestamp, "2999-01-01T00:00:00+00:00"),
        )
        ensure_scoped_alias(database, "gdpr-user", "comment:ONE")
        database.execute(
            "INSERT INTO ticker_comments(id,ticker,user_id,body,status,created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("comment-one", "ONE", "gdpr-user", "Public comment", "public", timestamp),
        )
        database.execute(
            "INSERT INTO user_positions(id,user_id,ticker,entry_price,entry_at,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'active',?,?)",
            ("position-one", "gdpr-user", "ONE", 1.25, timestamp, timestamp, timestamp),
        )
        database.execute(
            "INSERT INTO activity_events(profile_id,ticker,event_type,weight,created_at) "
            "VALUES(?,?,?,?,?)",
            ("u:gdpr-user", "ONE", "view", 1.0, timestamp),
        )
        database.execute(
            "INSERT INTO radar_seen(profile_id,ticker,last_seen_at) VALUES(?,?,?)",
            ("u:gdpr-user", "ONE", timestamp),
        )
        database.execute(
            "INSERT INTO pulse_profile_state(profile_id,ticker,entered_at,first_seen_at) "
            "VALUES(?,?,?,?)",
            ("u:gdpr-user", "ONE", timestamp, timestamp),
        )


def test_public_app_has_a_complete_privacy_surface() -> None:
    main_source = (ROOT / "src/runner_web/main.py").read_text()
    templates = "\n".join(path.read_text() for path in (ROOT / "web/templates").glob("*.html"))
    privacy_notice = (ROOT / "web/templates/privacy.html").read_text()

    assert 'VISITOR_COOKIE = "runner_visitor"' not in main_source
    assert "claim_visitor_profile" not in main_source
    assert 'max_age=365 * 24 * 3600' not in main_source
    assert '@app.get("/privacy"' in main_source
    assert '@app.get("/api/account/export"' in main_source
    assert '@app.post("/api/account/delete"' in main_source
    assert 'href="/privacy"' in templates
    for required_copy in (
        "Who controls your data",
        "Data we keep",
        "Why we use it",
        "Service providers and transfers",
        "How long we keep data",
        "Your rights",
        "privacy@rati.foundation",
    ):
        assert required_copy in privacy_notice


def test_export_and_delete_cover_account_content_and_old_tracking(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "gdpr-rights.db")
    init_db()
    _seed_user()

    exported = export_user_data("gdpr-user")

    assert exported["account"]["username"] == "member_gdpr"
    assert exported["comments"][0]["body"] == "Public comment"
    assert exported["positions"][0]["ticker"] == "ONE"
    assert exported["passkeys"] == []
    assert "token_hash" not in str(exported)

    deleted = delete_user_data("gdpr-user")

    assert deleted["deleted"] is True
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM ticker_comments").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM user_positions").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM radar_seen").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM pulse_profile_state").fetchone()[0] == 0


def test_passive_tracking_purge_removes_every_legacy_profile_table(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "gdpr-purge.db")
    init_db()
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO activity_events(profile_id,ticker,event_type,weight,created_at) "
            "VALUES('v:legacy','ONE','view',1,?)",
            (timestamp,),
        )
        database.execute(
            "INSERT INTO ticker_hearts(profile_id,ticker,created_at,updated_at) "
            "VALUES('v:legacy','ONE',?,?)",
            (timestamp, timestamp),
        )
        database.execute(
            "INSERT INTO radar_seen(profile_id,ticker,last_seen_at) VALUES('v:legacy','ONE',?)",
            (timestamp,),
        )
        database.execute(
            "INSERT INTO pulse_profile_state(profile_id,ticker,entered_at) "
            "VALUES('v:legacy','ONE',?)",
            (timestamp,),
        )
        database.execute(
            "INSERT INTO ticker_reactions(profile_id,ticker,reaction,created_at,updated_at) "
            "VALUES('v:legacy','ONE','bull',?,?)",
            (timestamp, timestamp),
        )

    result = purge_passive_tracking()

    assert result == {
        "activity_events": 1,
        "ticker_hearts": 1,
        "radar_seen": 1,
        "pulse_profile_state": 1,
        "ticker_reactions": 1,
    }


def test_public_aliases_are_stable_only_inside_one_thread(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "gdpr-aliases.db")
    init_db()
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("alias-one", "member_one", "Member", "active", timestamp),
        )
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("alias-two", "member_two", "Member", "active", timestamp),
        )
        first = ensure_scoped_alias(database, "alias-one", "comment:ONE")
        repeated = ensure_scoped_alias(database, "alias-one", "comment:ONE")
        other_thread = ensure_scoped_alias(database, "alias-one", "comment:TWO")
        call_identity = ensure_scoped_alias(database, "alias-one", "call:ONE")
        other_author = ensure_scoped_alias(database, "alias-two", "comment:ONE")

    assert first == repeated
    assert len({first, other_thread, call_identity, other_author}) == 4
    assert any(ord(character) > 10_000 for character in first)


def test_one_account_can_own_paid_animal_caller_ids_and_delete_them(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "gdpr-caller-ids.db")
    init_db()
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("caller-owner", "member_caller", "Caller", "active", timestamp),
        )

    first = claim_caller_id("caller-owner")
    assert first["claim_cost_cents"] == 0
    assert "-" in first["handle"]

    with pytest.raises(AdditionalCallerIdPaymentRequired):
        claim_caller_id("caller-owner")

    second = claim_caller_id("caller-owner", payment_reference="stripe:paid-once")
    assert second["claim_cost_cents"] > 0
    assert second["handle"] != first["handle"]

    with connection() as database:
        database.execute(
            "INSERT INTO community_calls(id,caller_identity_id,ticker,body,status,created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("call-one", second["id"], "ONE", "A public call", "public", timestamp),
        )

    deleted = delete_caller_id("caller-owner", second["id"])
    assert deleted["deleted"] is True
    assert deleted["handle"] == second["handle"]
    with connection() as database:
        tombstone = database.execute(
            "SELECT handle,user_id,status,payment_reference FROM caller_identities WHERE id=?",
            (second["id"],),
        ).fetchone()
        assert dict(tombstone) == {
            "handle": second["handle"],
            "user_id": None,
            "status": "tombstoned",
            "payment_reference": None,
        }
        assert database.execute(
            "SELECT COUNT(*) FROM community_calls WHERE caller_identity_id=?",
            (second["id"],),
        ).fetchone()[0] == 0


def test_openrouter_request_has_no_user_fingerprint() -> None:
    source = (ROOT / "src/runner_web/main.py").read_text()

    assert '"user": hashlib.sha256(user_id.encode()).hexdigest()[:32]' not in source


def test_compliance_runbook_covers_operations_not_just_ui() -> None:
    runbook = (ROOT / "docs/privacy-operations.md").read_text()

    for required_copy in (
        "Record of processing",
        "Retention schedule",
        "Data subject requests",
        "Processor register",
        "International transfers",
        "Security controls",
        "Personal data breach",
        "72 hours",
        "DPIA screening",
    ):
        assert required_copy in runbook
