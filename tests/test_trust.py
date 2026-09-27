from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from runner_web import db, trust

AT = datetime(2026, 9, 27, 12, tzinfo=UTC)
PERSON = {
    "login": "octocat",
    "id": 583231,
    "type": "User",
    "created_at": "2011-01-25T18:44:36Z",
    "public_repos": 8,
    "followers": 20000,
}


def test_the_published_record_commits_to_v1_and_its_digest_checks_out():
    record = trust.rules_record()

    assert record["current"]["version"] == "1.0.0"
    assert record["current"]["digest"].startswith("025c4cbb")
    assert "files" not in record["current"]  # the current version is only a commitment
    published = record["revealed"][0]
    assert published["version"] == "1.0.0" and published["verified"] is True
    assert "## Memecoins: nine standards" in published["rules_text"]


def test_a_tampered_file_no_longer_matches_its_digest():
    record = json.loads((trust.ASSETS / "trust-rules-1.0.0.json").read_text())
    record["files"]["RULES.md"] = record["files"]["RULES.md"].replace("30%", "60%")

    assert trust.recomputed_digest(record) != record["digest"]


@pytest.mark.parametrize(
    ("login", "user", "code"),
    [
        ("not a name", None, "invalid"),
        ("ghost-person", None, "missing"),
        ("github", {**PERSON, "type": "Organization"}, "organisation"),
        ("newbie", {**PERSON, "created_at": "2026-09-20T00:00:00Z"}, "young"),
        ("quiet", {**PERSON, "public_repos": 0, "followers": 0}, "inactive"),
    ],
)
def test_an_account_that_does_not_look_real_is_turned_away(login, user, code):
    result = trust.check_github_account(login, fetch=lambda _: user, at=AT)

    assert result == {"ok": False, "code": code}


def test_a_real_account_passes_with_its_id():
    result = trust.check_github_account("@octocat", fetch=lambda _: PERSON, at=AT)

    assert result["ok"] is True and result["github_id"] == 583231


def test_github_being_down_is_not_a_rejection_of_the_account():
    def down(_):
        raise OSError("timeout")

    assert trust.check_github_account("octocat", fetch=down, at=AT)["code"] == "unreachable"


@pytest.fixture
def client(tmp_path, monkeypatch):
    from runner_web import main

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "trust.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    monkeypatch.setattr(main, "enforce_rate", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "current_user", lambda *_: None)
    monkeypatch.setattr(
        trust, "fetch_github_user", lambda login: PERSON if login == "octocat" else None
    )
    return TestClient(main.app, base_url=main.RUNNERS_ORIGIN, follow_redirects=False)


def request_access(client, login, reason="Reviewing the rules"):
    from runner_web import main

    return client.post(
        "/trust/access",
        content=f"github={login}&reason={reason}",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "origin": main.RUNNERS_ORIGIN,
        },
    )


def test_the_trust_page_shows_the_commitment_and_the_form(client):
    page = client.get("/trust")

    assert page.status_code == 200
    assert "025c4cbb2e8a3a153953964b29364c745cb6039a8bf0d32bc01f40cd69b58976" in page.text
    assert "digest checks out" in page.text
    assert 'action="/trust/access"' in page.text


def test_a_request_is_recorded_once_per_account(client):
    first = request_access(client, "octocat")
    again = request_access(client, "octocat")

    assert (
        first.status_code == 303 and first.headers["location"] == "/trust?requested=octocat#access"
    )
    assert again.headers["location"] == "/trust?requested=octocat&already=1#access"
    with db.connection() as database:
        rows = trust.access_requests(database)
    assert [(row["github_login"], row["status"], row["reason"]) for row in rows] == [
        ("octocat", "pending", "Reviewing the rules")
    ]
    assert (
        "Request recorded for <strong>octocat</strong>"
        in client.get(first.headers["location"]).text
    )


def test_a_rejected_account_is_told_why_and_not_recorded(client):
    response = request_access(client, "ghost-person")

    assert response.headers["location"] == "/trust?error=missing#access"
    assert "No GitHub account has that name." in client.get(response.headers["location"]).text
    with db.connection() as database:
        assert trust.access_requests(database) == []


def test_a_link_cannot_put_arbitrary_words_on_the_page(client):
    page = client.get("/trust?error=Your+funds+are+frozen&requested=<script>")

    assert "Your funds are frozen" not in page.text
    assert "<script>" not in page.text.split('id="access"')[1]


def test_a_request_from_another_site_is_refused(client):
    response = client.post(
        "/trust/access",
        content="github=octocat",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://evil.example",
        },
    )

    assert response.status_code == 403


def test_trust_rati_chat_serves_the_page_at_its_root(client):
    from runner_web import main

    trust_site = TestClient(main.app, base_url=main.TRUST_ORIGIN, follow_redirects=False)

    page = trust_site.get("/")

    assert page.status_code == 200 and "The RATi Rules" in page.text


def test_only_operations_can_list_requests(client):
    assert client.get("/api/trust/access-requests").status_code == 404
