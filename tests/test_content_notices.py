from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from runner_web import db
from runner_web import main as web_main
from runner_web.content_notices import (
    disclosure_input,
    insert_content_notice,
    record_content_notice,
)
from runner_web.db import connection, init_db
from runner_web.flash_wallet import credit_flash
from runner_web.privacy import delete_user_content, delete_user_data, export_user_data


@pytest.fixture
def notice_db(tmp_path, monkeypatch):
    path = tmp_path / "notices.db"
    monkeypatch.setattr(db, "DATABASE_PATH", path)
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setattr(web_main, "_known_ticker", lambda _: True)
    monkeypatch.setattr(web_main, "_openrouter_api_key", lambda: "fake-key")
    web_main.RATE_LIMITS.clear()
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    init_db()
    stamp = datetime.now(UTC)
    with connection() as database:
        for user in ("alice", "bob"):
            database.execute(
                "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
                (user, user, user.title(), "active", stamp.isoformat()),
            )
            database.execute(
                "INSERT INTO sessions(token_hash,user_id,created_at,expires_at,authenticated_at) "
                "VALUES(?,?,?,?,?)",
                (
                    web_main.token_hash(user),
                    user,
                    stamp.isoformat(),
                    (stamp + timedelta(days=1)).isoformat(),
                    stamp.isoformat(),
                ),
            )
        for suffix, ticker, visibility in (
            ("stock", "FIX", "public"),
            ("sport", "sports:game1", "public"),
            ("private", "FIX", "private"),
        ):
            evidence = (
                {"subject_type": "sports_game", "event_id": "game1"} if suffix == "sport" else {}
            )
            database.execute(
                """
                INSERT INTO research_commissions(
                    id,public_id,user_id,ticker,evidence_key,status,requested_model,headline,
                    summary,evidence_snapshot_json,visibility,created_at,updated_at
                ) VALUES(?,?,?,?,?,'complete',?,?,?,?,?,?,?)
                """,
                (
                    suffix,
                    f"public-{suffix}",
                    "alice",
                    ticker,
                    suffix,
                    "test/model",
                    "Original headline",
                    "Original summary",
                    json.dumps(evidence),
                    visibility,
                    stamp.isoformat(),
                    stamp.isoformat(),
                ),
            )
        for comment_id, subject, key in (
            ("comment-stock", "stock", "FIX"),
            ("comment-sport", "sports_game", "game1"),
        ):
            database.execute(
                """
                INSERT INTO ticker_comments(id,ticker,subject_kind,subject_key,user_id,
                    body,status,created_at,source,generation_model)
                VALUES(?,?,?,?,?,?,'public',?,'ai_avatar','test/model')
                """,
                (comment_id, key, subject, key, "alice", "Original comment", stamp.isoformat()),
            )
        credit_flash(database, "alice", 100, kind="test", reference_id="notice-tests")
    return path


def _client(user=None):
    client = TestClient(web_main.app, base_url=web_main.APP_ORIGIN)
    if user:
        client.cookies.set(web_main.SESSION_COOKIE, user)
    return client


def _cli(path, *args):
    return subprocess.run(
        [sys.executable, "-m", "runner_web.content_notice", *args],
        cwd=Path(__file__).resolve().parents[1],
        env={
            **os.environ,
            "DATABASE_PATH": str(path),
            "DATABASE_URL": "",
            "REQUIRE_DATABASE_URL": "0",
            "PYTHONPATH": "src",
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_operator_cli_keeps_original_and_both_corrections_current(notice_db):
    before = web_main._public_research_report_data("public-stock")["report"]
    assert before["corrections"] == []
    for text, reason in (
        ("Revenue was $2 million.", "The source used millions."),
        ("Revenue was $2.1 million.", "The filing was amended."),
    ):
        result = _cli(
            notice_db,
            "report",
            "public-stock",
            "--kind",
            "correction",
            "--text",
            text,
            "--reason",
            reason,
        )
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout)
        assert receipt["text"] == text and receipt["reason"] == reason
        assert receipt["recorded_by"] == "operator"
        assert datetime.fromisoformat(receipt["created_at"]).tzinfo
    after = web_main._public_research_report_data("public-stock")["report"]
    assert after["headline"] == before["headline"] == "Original headline"
    assert after["summary"] == "Original summary"
    assert len(after["corrections"]) == 2
    assert after["corrections"][0]["text"] == "Revenue was $2 million."


@pytest.mark.parametrize(
    "args",
    [
        ("report", "missing", "--kind", "holdings", "--text", "Publisher holds shares."),
        ("comment", "missing", "--kind", "sponsorship", "--text", "Issuer paid publisher."),
        ("report", "public-stock", "--kind", "correction", "--text", "Fixed claim."),
        (
            "report",
            "public-stock",
            "--kind",
            "correction",
            "--text",
            "Fixed claim.",
            "--reason",
            "   ",
        ),
        ("report", "public-stock", "--kind", "holdings", "--text", "   "),
    ],
)
def test_operator_cli_rejects_invalid_records_without_writes(notice_db, args):
    result = _cli(notice_db, *args)
    assert result.returncode == 2
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM content_notices").fetchone()[0] == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/research/public-stock/disclosures",
        "/api/research/public-sport/disclosures",
        "/api/comments/comment-stock/disclosures",
        "/api/comments/comment-sport/disclosures",
    ],
)
def test_author_disclosures_require_ownership_and_origin_and_retry_safely(notice_db, endpoint):
    payload = {"disclosure_kind": "sponsorship", "disclosure": "The issuer paid me for this post."}
    headers = {"Origin": web_main.APP_ORIGIN}
    client = _client()
    try:
        assert client.post(endpoint, headers=headers, json=payload).status_code == 401
        client.cookies.set(web_main.SESSION_COOKIE, "bob")
        assert client.post(endpoint, headers=headers, json=payload).status_code == 404
        client.cookies.set(web_main.SESSION_COOKIE, "alice")
        assert (
            client.post(
                endpoint, headers={"Origin": "https://other.test"}, json=payload
            ).status_code
            == 403
        )
        assert (
            client.post(
                endpoint, headers=headers, json={"disclosure_kind": "sponsorship"}
            ).status_code
            == 422
        )
        first = client.post(endpoint, headers=headers, json=payload)
        assert first.status_code == 200
        second = client.post(endpoint, headers=headers, json=payload)
        assert first.json() == second.json()
        assert first.json()["notice"]["recorded_by"] == "author"
        assert len(first.json()["disclosures"]) == 1
        assert set(first.json()["notice"]) == {
            "id",
            "kind",
            "label",
            "text",
            "reason",
            "created_at",
            "recorded_by",
        }
    finally:
        client.close()


def test_concurrent_author_retry_stores_one_notice(notice_db):
    def save(_):
        with connection() as database:
            return insert_content_notice(
                database,
                "report",
                "stock",
                kind="holdings",
                text="I hold shares.",
                recorded_by="author",
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        notices = list(pool.map(save, range(2)))
    assert notices[0] == notices[1]


def test_private_report_notice_follows_report_visibility(notice_db):
    record_content_notice(
        "report", "public-private", kind="holdings", text="Publisher holds shares."
    )
    assert web_main._public_research_report_data("public-private") == {"report": None}
    assert len(web_main.get_commission("public-private")["disclosures"]) == 1
    client = _client()
    try:
        assert client.get("/research/public-private").status_code == 404
    finally:
        client.close()


def test_cached_comment_page_reads_current_notices_and_deletion(notice_db, monkeypatch):
    old = web_main.comments_for_ticker("FIX")
    monkeypatch.setattr(
        web_main,
        "_public_screen_data",
        lambda *_: {
            "found": True,
            "comments": old,
            "comment_count": len(old),
        },
    )
    assert web_main._public_ticker_page_data("FIX")["comments"][0]["disclosures"] == []
    record_content_notice(
        "comment", "comment-stock", kind="compensation", text="Issuer paid the author."
    )
    current = web_main._public_ticker_page_data("FIX")["comments"][0]
    assert current["disclosures"][0]["text"] == "Issuer paid the author."
    record_content_notice(
        "comment",
        "comment-stock",
        kind="correction",
        text="The filing date was Monday.",
        reason="The first date was wrong.",
    )
    assert len(web_main.alpha_comments_data()[0]["corrections"]) == 1
    delete_user_content("alice")
    assert web_main._public_ticker_page_data("FIX")["comments"] == []


@pytest.mark.parametrize("delete", [delete_user_content, delete_user_data])
def test_export_and_delete_include_notices_and_refresh_report(notice_db, delete):
    record_content_notice("comment", "comment-stock", kind="holdings", text="I hold shares.")
    record_content_notice(
        "report",
        "public-stock",
        kind="issuer_relationship",
        text="The publisher advises the issuer.",
    )
    assert len(export_user_data("alice")["content_notices"]) == 2
    assert export_user_data("bob")["content_notices"] == []
    assert web_main._public_research_report_data("public-stock")["report"]["disclosures"]
    delete("alice")
    assert web_main._public_research_report_data("public-stock") == {"report": None}
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM content_notices").fetchone()[0] == 0


@pytest.mark.parametrize("subject", ["stock", "game"])
def test_generated_comment_stores_disclosure_separately_from_model_input(
    notice_db, monkeypatch, subject
):
    seen = []

    def generate(key, *, avatar):
        seen.append((key, avatar))
        return "Generated from source evidence.", "test/model"

    monkeypatch.setattr(web_main, "_generate_ticker_comment_text", generate)
    monkeypatch.setattr(web_main, "_generate_sports_comment_text", generate)
    monkeypatch.setattr(web_main, "sports_event", lambda _: {"id": "game1"})
    client = _client("alice")
    try:
        response = client.post(
            f"/api/comments/{subject}/{'FIX' if subject == 'stock' else 'game1'}",
            headers={
                "Origin": web_main.APP_ORIGIN,
                "Idempotency-Key": f"disclosure-{subject}-request",
            },
            json={"disclosure_kind": "holdings", "disclosure": "PRIVATE-RELATIONSHIP-SENTINEL"},
        )
        assert response.status_code == 201, response.text
        comment = response.json()["comment"]
        assert comment["disclosures"][0]["text"] == "PRIVATE-RELATIONSHIP-SENTINEL"
        assert "PRIVATE-RELATIONSHIP-SENTINEL" not in json.dumps(seen)
        assert "PRIVATE-RELATIONSHIP-SENTINEL" not in comment["body"]
        replay = client.post(
            f"/api/comments/{subject}/{'FIX' if subject == 'stock' else 'game1'}",
            headers={
                "Origin": web_main.APP_ORIGIN,
                "Idempotency-Key": f"disclosure-{subject}-request",
            },
            json={"disclosure_kind": "compensation", "disclosure": "A different retry body."},
        )
        assert replay.json()["comment"] == comment
        assert len(seen) == 1
        assert (
            client.delete(
                f"/api/comments/{comment['id']}", headers={"Origin": web_main.APP_ORIGIN}
            ).status_code
            == 200
        )
        with connection() as database:
            assert (
                database.execute(
                    "SELECT COUNT(*) FROM content_notices WHERE comment_id=?", (comment["id"],)
                ).fetchone()[0]
                == 0
            )
    finally:
        client.close()


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"disclosure": "Text"},
        {"disclosure_kind": "unknown", "disclosure": "Text"},
        {"disclosure_kind": "holdings", "disclosure": "x" * 1001},
    ],
)
def test_disclosure_fields_are_strict(value):
    with pytest.raises(ValueError):
        disclosure_input(value)


@pytest.mark.parametrize("subject", ["stock", "sports_game"])
def test_stored_disclosures_stay_out_of_later_comment_prompts(notice_db, monkeypatch, subject):
    sentinel = "STORED-RELATIONSHIP-SENTINEL"
    record_content_notice(
        "comment",
        "comment-stock" if subject == "stock" else "comment-sport",
        kind="holdings",
        text=sentinel,
    )
    record_content_notice("report", "public-stock", kind="compensation", text=sentinel)
    captured = []

    def generate(evidence, **kwargs):
        captured.append({"evidence": evidence, **kwargs})
        return "A source-based comment.", "test/model"

    monkeypatch.setattr(web_main, "_generate_comment_from_evidence", generate)
    monkeypatch.setattr(web_main, "ticker_detail_data", lambda _: {"ticker": "FIX"})
    monkeypatch.setattr(web_main, "_ticker_summary", lambda _: None)
    monkeypatch.setattr(
        web_main, "daily_report_for_ticker", lambda _: web_main.get_commission("public-stock")
    )
    monkeypatch.setattr(web_main, "sports_event", lambda _: {"id": "game1"})
    if subject == "stock":
        web_main._generate_ticker_comment_text("FIX", avatar={"name": "Test avatar"})
    else:
        web_main._generate_sports_comment_text("game1", avatar={"name": "Test avatar"})
    assert captured and sentinel not in json.dumps(captured)


def test_author_unicode_disclosure_uses_bounded_duplicate_key(notice_db):
    with connection() as database:
        first = insert_content_notice(
            database, "report", "stock", kind="holdings", text="💎" * 1000, recorded_by="author"
        )
        second = insert_content_notice(
            database, "report", "stock", kind="holdings", text="💎" * 1000, recorded_by="author"
        )
        row = database.execute(
            "SELECT dedup_key FROM content_notices WHERE id=?", (first["id"],)
        ).fetchone()
    assert first == second
    assert len(row["dedup_key"]) == 64


@pytest.mark.parametrize(
    "correction,disclosure", [(False, False), (True, False), (False, True), (True, True)]
)
def test_report_share_metadata_and_card_carry_current_notices(
    notice_db, monkeypatch, correction, disclosure
):
    original = web_main.get_commission("public-stock")
    current_claim = "Revenue was $2.1 million for the quarter."
    if correction:
        record_content_notice(
            "report",
            "public-stock",
            kind="correction",
            text="Revenue was $2 million.",
            reason="The first unit was wrong.",
        )
        record_content_notice(
            "report",
            "public-stock",
            kind="correction",
            text=current_claim,
            reason="The filing was amended.",
        )
    if disclosure:
        record_content_notice(
            "report", "public-stock", kind="sponsorship", text="The issuer paid the publisher."
        )
    report = web_main.get_commission("public-stock")
    labels = (["Correction"] if correction else []) + (["Disclosure"] if disclosure else [])
    assert report["share_notice_label"] == " · ".join(labels)
    assert report["headline"] == original["headline"] == "Original headline"
    assert report["summary"] == original["summary"] == "Original summary"
    if correction:
        assert report["share_summary"].endswith(current_claim)
        assert "Original" not in report["share_title"] + report["share_summary"]
    else:
        assert report["share_title"].endswith("Original headline")
        assert report["share_summary"].endswith("Original summary")
    for label in labels:
        assert label in report["share_title"] and label in report["share_summary"]
    drawn_text = []
    original_text = web_main.ImageDraw.ImageDraw.text

    def draw_text(self, xy, text, *args, **kwargs):
        drawn_text.append(str(text))
        return original_text(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(web_main.ImageDraw.ImageDraw, "text", draw_text)
    client = _client()
    try:
        response = client.get("/research/public-stock/card.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert "no-store" in response.headers["cache-control"]
        with web_main.Image.open(io.BytesIO(response.content)) as image:
            assert image.size == (1200, 630)
            if labels:
                assert image.getpixel((100, 275)) == (59, 41, 19)
        visible = " ".join(" ".join(drawn_text).split())
        for label in labels:
            assert label in visible
        if correction:
            assert current_claim in visible
            assert "Original headline" not in visible
        else:
            assert "Original headline" in visible
    finally:
        client.close()


def test_report_card_changes_after_correction_and_respects_private_access(notice_db):
    client = _client()
    try:
        first = client.get("/research/public-stock/card.png")
        record_content_notice(
            "report",
            "public-stock",
            kind="correction",
            text="The filing date was Monday.",
            reason="The day was incorrect.",
        )
        second = client.get("/research/public-stock/card.png")
        assert first.content != second.content
        assert client.get("/research/public-private/card.png").status_code == 404
        client.cookies.set(web_main.SESSION_COOKIE, "alice")
        assert client.get("/research/public-private/card.png").status_code == 200
    finally:
        client.close()


def test_report_card_fallback_font_keeps_requested_size(monkeypatch):
    original = web_main.ImageFont.truetype

    def missing_named_font(name, *args, **kwargs):
        if isinstance(name, str):
            raise OSError("The preferred font is unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(web_main.ImageFont, "truetype", missing_named_font)
    bounds = web_main.font(37, True).getbbox("Revenue was $2.1 million.")
    assert bounds[3] - bounds[1] >= 30
