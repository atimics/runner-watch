from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.caller_ids import ensure_caller_identity
from runner_web.db import connection, init_db
from runner_web.privacy import (
    delete_user_content,
    delete_user_data,
    export_user_data,
    purge_passive_tracking,
    user_data_summary,
)
from runner_web.pseudonyms import ensure_comment_avatar, ensure_scoped_alias

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
        ensure_comment_avatar(database, "gdpr-user")
        database.execute(
            "INSERT INTO ticker_comments(id,ticker,user_id,body,status,created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("comment-one", "ONE", "gdpr-user", "Public comment", "public", timestamp),
        )
        database.execute(
            "INSERT INTO sports_events("
            "id,provider,external_id,league,name,start_time,status,status_detail,"
            "home_team_id,home_team_name,home_abbreviation,away_team_id,away_team_name,"
            "away_abbreviation,source_url,first_collected_at,last_collected_at) "
            "VALUES(?,?,?,?,?,?,'pre','Scheduled',?,?,?,?,?,?,?,?,?)",
            (
                "mlb:privacy-game",
                "test",
                "privacy-game",
                "mlb",
                "Away at Home",
                timestamp,
                "home",
                "Home",
                "HOM",
                "away",
                "Away",
                "AWY",
                "https://example.test/game",
                timestamp,
                timestamp,
            ),
        )
        database.execute(
            "INSERT INTO sports_comments(id,event_id,user_id,body,status,created_at,source) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "sports-comment-one",
                "mlb:privacy-game",
                "gdpr-user",
                "Sports comment",
                "public",
                timestamp,
                "user",
            ),
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
    assert '@app.post("/api/account/data/delete-cloud-copy"' in main_source
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


def test_local_vault_checks_encryption_before_cloud_deletion() -> None:
    source = (ROOT / "web/static/data-vault.js").read_text()
    privacy_notice = (ROOT / "web/templates/privacy.html").read_text()

    for required in (
        "AES-GCM",
        "PBKDF2",
        "indexedDB",
        "DELETE LOCAL COPY",
        "50 * 1024 * 1024",
    ):
        assert required in source
    assert source.index("const checked = await decryptVault") < source.index(
        "fetch('/api/account/data/delete-cloud-copy'"
    )
    assert "innerHTML" not in source
    assert "Move saved data off the cloud" in privacy_notice
    assert "passphrase and key are never sent to RATi" in privacy_notice


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
    assert exported["sports_comments"][0]["body"] == "Sports comment"
    assert exported["comment_avatar"][0]["ability_id"]
    assert exported["comment_avatar"][0]["seed"]
    assert exported["positions"][0]["ticker"] == "ONE"
    assert exported["caller_identities"][0]["handle"] == caller["handle"]
    assert exported["community_calls"][0]["public_id"] == "gdpr-public"
    assert exported["passkeys"] == []
    assert "token_hash" not in str(exported)
    assert "registration_invite_hash" not in exported["account"]

    deleted = delete_user_data("gdpr-user")

    assert deleted["deleted"] is True
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM ticker_comments").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM sports_comments").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM comment_avatars").fetchone()[0] == 0
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


def test_move_to_device_deletes_content_but_keeps_the_working_account(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "local-vault-move.db")
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
                "move-call",
                "move-public",
                "gdpr-user",
                caller["id"],
                "ONE",
                1.0,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        database.execute(
            "INSERT INTO flash_wallets(user_id,balance,created_at,updated_at) "
            "VALUES(?,?,?,?)",
            ("gdpr-user", 250, timestamp, timestamp),
        )

    summary = user_data_summary("gdpr-user")
    assert summary == {
        "item_count": 4,
        "groups": {"Posts and Calls": 3, "Private work": 1, "Research": 0},
    }

    result = delete_user_content("gdpr-user")

    assert result["deleted"] is True
    assert result["items_deleted"] >= 3
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM comment_avatars").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM flash_wallets").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM ticker_comments").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM sports_comments").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM user_positions").fetchone()[0] == 0
        assert database.execute("SELECT COUNT(*) FROM community_calls").fetchone()[0] == 0
        identity = database.execute(
            "SELECT user_id,status FROM caller_identities WHERE id=?", (caller["id"],)
        ).fetchone()
        assert dict(identity) == {"user_id": "gdpr-user", "status": "active"}


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


def test_comment_avatar_is_stable_across_threads_while_call_identity_stays_separate(
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
        first = ensure_comment_avatar(database, "alias-one")
        repeated = ensure_comment_avatar(database, "alias-one")
        call_identity = ensure_scoped_alias(database, "alias-one", "call:ONE")
        other_author = ensure_comment_avatar(database, "alias-two")

    assert first == repeated
    assert first["name"] != other_author["name"]
    assert first["name"] != call_identity
    assert first["ability_id"]
    assert first["level"] == 1


def test_privacy_notice_explains_the_persistent_avatar_boundary() -> None:
    notice = (ROOT / "web/templates/privacy.html").read_text()

    assert "intentionally links comments across ticker and sports threads" in notice
    assert "readers can link those comments together" in notice
    assert "pseudonymity, not anonymity from RATi" in notice


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


def test_production_uses_privacy_safe_application_access_logs() -> None:
    assert "--no-access-log" in (ROOT / "fly.toml").read_text()
    assert '"--no-access-log"' in (ROOT / "Dockerfile").read_text()
    source = (ROOT / "src/runner_web/main.py").read_text()
    assert "request_complete method=%s path=%s status=%s duration_ms=%.1f" in source


def test_runtime_container_is_non_root_and_uses_immutable_base_images() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "USER runner" in dockerfile
    assert dockerfile.count("@sha256:") == 3


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
