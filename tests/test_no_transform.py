from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from runner_web import db
from runner_web import main as web_main


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, "no-transform"),
        ("", "no-transform"),
        ("private, max-age=15", "private, max-age=15, no-transform"),
        ("no-store, No-Transform", "no-store, No-Transform"),
    ],
)
def test_no_transform_is_added_without_dropping_what_was_there(given, expected) -> None:
    assert web_main._with_no_transform(given) == expected


def test_html_pages_ask_not_to_be_rewritten_and_json_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "no-transform.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    monkeypatch.setattr(web_main, "enforce_rate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_main, "current_user", lambda *_: None)
    client = TestClient(web_main.app, base_url=web_main.APP_ORIGIN)
    try:
        page = client.get("/login")
        assert page.headers["content-type"].startswith("text/html")
        assert "no-transform" in page.headers["Cache-Control"]
        data = client.get("/api/version")
        assert "no-transform" not in data.headers.get("Cache-Control", "")
        asset = client.get("/static/market-screen.js")
        assert asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    finally:
        client.close()
