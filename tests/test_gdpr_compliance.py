from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.caller_ids import ensure_caller_identity
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
            "INSERT INTO user_positions("
            "id,user_id,ticker,entry_price,entry_at,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'active',?,?)",
            ("position-one", "gdpr-user", "ONE", 1.25, timestamp, timestamp, timestamp),
        )


def test_public_app_has_a_complete_privacy_surface() -> None:
    main_source = (ROOT / "src/runner_web/main.py").read_text()
    templates = "\n".join(path.read_text() for path in (ROOT / "web/templates").glob("*.html"))
    privacy_notice = (ROOT / "web/templates/privacy.html").read_text()

    assert 'VISITOR_COOKIE = "runner_visitor"' not in main_source
    assert "claim_visitor_profile" not in main_source
    assert "TickerCommentPayload" not in main_source
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
        "privacy@cenetex.com",
    ):
        assert required_copy in privacy_notice


def test_export_and_delete_cover_account_content_and_leave_anonymous_tombstone(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "gdpr-rights.db")
    init_db()
    _seed_user()
    caller = ensure_caller_identity("gdpr-user")
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO community_calls("
            "id,public_id,user_id,caller_identity_id,ticker,entry_price,entry_at,"
            "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'active',?,?)",
            (
                "gdpr-call", "gdpr-public", "gdpr-user", caller["id"], "ONE", 1.0,
                timestamp, timestamp, timestamp,
            ),
        )

    exported = export_user_data("gdpr-user")

    assert exported["account"]["username"] == "member_gdpr"
    assert exported["comments"][0]["body"] == "Public comment"
    assert exported["positions"][0]["ticker"] == "ONE"
    assert exported["caller_identities"][0]["handle"] == caller["handle"]
    assert exported["community_calls"][0]["public_id"] == "gdpr-public"
    assert exported["passkeys"] == []
    assert "token_hash" not in str(exported)

    deleted = delete_user_data("gdpr-user")

    assert deleted["deleted"] is True
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM ticker_comments").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM user_positions").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM community_calls").fetchone()[0] == 0
        tombstone = database.execute(
            "SELECT handle,user_id,status,claim_cost_cents,payment_reference,claimed_at "
            "FROM caller_identities WHERE id=?",
            (caller["id"],),
        ).fetchone()
        assert dict(tombstone) == {
            "handle": caller["handle"],
            "user_id": None,
            "status": "tombstoned",
            "claim_cost_cents": None,
            "payment_reference": None,
            "claimed_at": None,
        }


def test_passive_tracking_schema_is_removed_and_purge_stays_safe(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "gdpr-purge.db")
    init_db()
    with connection() as database:
        tables = {
            row["name"]
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert {
        "activity_events",
        "ticker_hearts",
        "radar_seen",
        "pulse_profile_state",
        "ticker_reactions",
        "comment_pseudonyms",
    }.isdisjoint(tables)

    result = purge_passive_tracking()

    assert result == {
        "activity_events": 0,
        "ticker_hearts": 0,
        "radar_seen": 0,
        "pulse_profile_state": 0,
        "ticker_reactions": 0,
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


def test_privacy_notice_explains_the_thread_alias_boundary() -> None:
    notice = (ROOT / "web/templates/privacy.html").read_text()

    assert "Within one thread, the same emoji means the same account." in notice
    assert "Across different threads, different emojis do not imply different people." in notice


def test_one_account_gets_one_automatic_anonymous_call_identity(
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

    first = ensure_caller_identity("caller-owner")
    assert "-" in first["handle"]
    assert ensure_caller_identity("caller-owner") == first
    with connection() as database:
        assert database.execute(
            "SELECT COUNT(*) FROM caller_identities WHERE user_id=? AND status='active'",
            ("caller-owner",),
        ).fetchone()[0] == 1
        assert database.execute(
            "SELECT COUNT(*) FROM caller_identity_claims WHERE user_id=?",
            ("caller-owner",),
        ).fetchone()[0] == 0


def test_openrouter_request_has_no_user_fingerprint() -> None:
    source = (ROOT / "src/runner_web/main.py").read_text()

    assert '"user": hashlib.sha256(user_id.encode()).hexdigest()[:32]' not in source


def test_production_does_not_write_web_access_logs() -> None:
    assert "--no-access-log" in (ROOT / "fly.toml").read_text()
    assert '"--no-access-log"' in (ROOT / "Dockerfile").read_text()


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
