from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from runner_web import audits, db

UUID = "3f1c1c1e-1b7a-4c3e-9a55-0d4f6a1b2c3d"
WALLET = "FANZg2wvKX7277gD2vfSxvFZTSamoUZUGuAQc6CP7JAT"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def site(tmp_path, monkeypatch):
    from runner_web import main

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "trust.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    monkeypatch.setattr(main, "enforce_rate", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "current_user", lambda *_: None)
    monkeypatch.setattr(audits, "ASSETS", tmp_path / "audits")
    return (
        TestClient(main.app, base_url=main.TRUST_ORIGIN, follow_redirects=False),
        main,
        tmp_path / "audits",
    )


def write_draft(root, vault=True):
    folder = root / "draft" / UUID
    folder.mkdir(parents=True)
    status = {
        "uuid": UUID,
        "phase": "draft",
        "checked": "2026-09-29",
        "protocol": {"version": "2.6.0", "digest": "d" * 64},
        "packet_digest": "p" * 64,
        "commit_sha256": "c" * 64,
        "record_sha256": "r" * 64,
        "findings": {
            "total": 14,
            "open": 12,
            "fixed": 1,
            "acknowledged": 1,
            "broken": 0,
            "by_severity": {},
        },
        "gates": ["phase not done: manual_review"],
        "reviewers": [{"wallet": WALLET, "findings": ["001-C1"], "counts": True}],
        "revisions": 0,
        "disclosure": {"policy": "public-after-fix", "max_wait_days": 90},
    }
    (folder / "status.json").write_text(json.dumps(status))
    if vault:
        (folder / "vault.html").write_text("<script>const VAULT = {}</script>")


def write_final(root, address="A" * 43, tamper=False):
    folder = root / "final" / address
    folder.mkdir(parents=True)
    files = {
        "report.md": "# Audit 001\n",
        "attestation.json": json.dumps(
            {"title": "Fishbowl", "findings": {"total": 2, "fixed": 2, "acknowledged": 0}}
        ),
    }
    for name, text in files.items():
        (folder / name).write_text(text)
    manifest = {
        "audit": "001",
        "signer": "Jon",
        "signed": "2026-09-29",
        "protocol": {"version": "2.6.0"},
        "files": {n: sha(t) for n, t in files.items()},
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (folder / "allowed_signers").write_text('Jon namespaces="ratiaudit" ssh-ed25519 AAAA\n')
    if tamper:
        (folder / "report.md").write_text("# softened\n")
    return address


def test_a_draft_page_shows_progress_and_never_code(site):
    client, _main, root = site
    write_draft(root)
    page = client.get(f"/draft/{UUID}")
    assert page.status_code == 200
    assert "An audit in progress" in page.text and "phase not done: manual_review" in page.text
    assert WALLET in page.text and "Open the sealed report" in page.text
    assert "github.com" not in page.text


def test_a_draft_serves_its_status_as_json_and_its_vault_with_its_own_csp(site):
    client, main, root = site
    write_draft(root)
    status = client.get(f"/draft/{UUID}/status.json")
    assert (
        status.status_code == 200
        and status.json()["findings"]["total"] == 14
        and "has_vault" not in status.json()
    )

    vault = client.get(f"/draft/{UUID}/vault")
    assert vault.status_code == 200 and "const VAULT" in vault.text
    csp = vault.headers["content-security-policy"]
    assert (
        "https://cdnjs.cloudflare.com" in csp
        and "'unsafe-inline'" in csp
        and "connect-src 'none'" in csp
    )
    assert vault.headers["cache-control"] == "no-store"

    other = client.get(f"/draft/{UUID}")
    assert (
        "cdnjs" not in other.headers["content-security-policy"]
    )  # every other page keeps the strict policy


def test_a_draft_without_a_vault_does_not_offer_one(site):
    client, _main, root = site
    write_draft(root, vault=False)
    assert "Open the sealed report" not in client.get(f"/draft/{UUID}").text
    assert client.get(f"/draft/{UUID}/vault").status_code == 404


def test_unknown_and_malformed_drafts_are_not_found(site):
    client, _main, root = site
    write_draft(root)
    assert client.get("/draft/00000000-0000-4000-8000-000000000000").status_code == 404
    assert client.get("/draft/not-a-uuid").status_code == 404
    assert client.get("/draft/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_a_final_bundle_page_lists_files_and_their_hashes(site):
    client, _main, root = site
    address = write_final(root)
    page = client.get(f"/{address}")
    assert page.status_code == 200 and "Match their hashes" in page.text and "Fishbowl" in page.text
    assert f"/{address}/report.md" in page.text
    body = client.get(f"/{address}/report.md")
    assert body.status_code == 200 and body.text == "# Audit 001\n"
    assert body.headers["content-type"].startswith("text/markdown")


def test_a_changed_file_is_shown_as_not_matching(site):
    client, _main, root = site
    address = write_final(root, tamper=True)
    page = client.get(f"/{address}")
    assert (
        page.status_code == 200
        and "Do not match their hashes" in page.text
        and "does not match" in page.text
    )


def test_only_listed_bundle_files_are_served(site):
    client, _main, root = site
    address = write_final(root)
    (root / "final" / address / "secret.txt").write_text("nope")
    assert client.get(f"/{address}/secret.txt").status_code == 404
    assert client.get(f"/{address}/manifest.json").status_code == 200
    assert client.get("/" + "B" * 43).status_code == 404
    assert client.get("/" + "b" * 63).status_code == 404


def test_audits_exist_only_on_the_trust_host(site):
    from runner_web import main

    client, _main, root = site
    address = write_final(root)
    write_draft(root)
    runners = TestClient(main.app, base_url=main.RUNNERS_ORIGIN, follow_redirects=False)
    assert runners.get(f"/{address}").status_code == 404
    assert runners.get(f"/draft/{UUID}").status_code == 404


def test_the_root_route_does_not_shadow_the_real_pages(site):
    client, _main, _root = site
    assert client.get("/").status_code == 200  # the trust page
    assert client.get("/rules.schema.json").status_code == 200
    health = client.get("/health")
    assert health.status_code == 200 and health.json() != {"detail": "Not found"}
