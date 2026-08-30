from __future__ import annotations

import json
import re
import runpy
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from pytest import MonkeyPatch
from starlette.requests import Request

from runner_web import db
from runner_web import main as web_main
from runner_web.db import init_db
from runner_web.live_screens import public_dynamic_screen_paths


def _manifest_database() -> sqlite3.Connection:
    database = sqlite3.connect(":memory:")
    database.executescript(
        """
        CREATE TABLE scan_snapshots(ticker TEXT, captured_at TEXT);
        CREATE TABLE caller_identities(id TEXT, handle TEXT, status TEXT);
        CREATE TABLE community_calls(caller_identity_id TEXT, updated_at TEXT);
        CREATE TABLE research_commissions(
            public_id TEXT, status TEXT, visibility TEXT, published_at TEXT,
            completed_at TEXT, created_at TEXT
        );
        CREATE TABLE sports_events(id TEXT, status TEXT, start_time TEXT);
        """
    )
    return database


def test_public_dynamic_screen_paths_uses_latest_safe_public_records() -> None:
    with closing(_manifest_database()) as database:
        database.executescript(
            """
            INSERT INTO scan_snapshots VALUES ('OLD', '2026-01-01T00:00:00Z');
            INSERT INTO scan_snapshots VALUES ('NEW.A', '2026-01-02T00:00:00Z');
            INSERT INTO caller_identities VALUES ('caller-1', 'steady-ibis', 'active');
            INSERT INTO caller_identities VALUES ('caller-2', 'retired-ibis', 'tombstoned');
            INSERT INTO community_calls VALUES ('caller-1', '2026-01-01T00:00:00Z');
            INSERT INTO community_calls VALUES ('caller-2', '2026-01-02T00:00:00Z');
            INSERT INTO research_commissions VALUES (
                'public-report', 'complete', 'public', '2026-01-01T00:00:00Z', NULL,
                '2026-01-01T00:00:00Z'
            );
            INSERT INTO research_commissions VALUES (
                'private-report', 'complete', 'private', '2026-01-02T00:00:00Z', NULL,
                '2026-01-02T00:00:00Z'
            );
            INSERT INTO sports_events VALUES ('mlb:past', 'post', '2026-01-03T00:00:00Z');
            INSERT INTO sports_events VALUES ('mlb:next', 'pre', '2026-01-04T00:00:00Z');
            """
        )

        assert public_dynamic_screen_paths(database) == {
            "ticker": "/t/NEW.A",
            "caller": "/u/steady-ibis",
            "research": "/research/public-report",
            "sports_game": "/game/mlb:next",
        }


def test_public_dynamic_screen_paths_reports_missing_fixtures() -> None:
    with closing(_manifest_database()) as database:
        assert public_dynamic_screen_paths(database) == {
            "ticker": None,
            "caller": None,
            "research": None,
            "sports_game": None,
        }


def test_live_screen_manifest_returns_only_public_path_keys(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "screen-manifest.db")
    monkeypatch.setattr(web_main, "rate_limit_allowed", lambda *args, **kwargs: True)
    init_db()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/smoke/screens",
            "headers": [],
            "client": ("127.0.0.1", 4301),
        }
    )

    response = web_main.live_screen_manifest(request)
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert set(payload["dynamic"]) == {
        "ticker",
        "caller",
        "research",
        "sports_game",
    }


def test_live_screen_sweep_defaults_to_one_second(monkeypatch: MonkeyPatch) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "test-live-screens"
    namespace = runpy.run_path(str(script))
    monkeypatch.delenv("LIVE_SCREEN_SLOW_MS", raising=False)
    monkeypatch.setattr(sys, "argv", [str(script)])

    assert namespace["parse_args"]().slow_ms == 1_000


def test_privacy_screen_heading_matches_the_template() -> None:
    root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(str(root / "scripts" / "test-live-screens"))
    privacy_screen = next(screen for screen in namespace["SCREENS"] if screen.key == "privacy")
    privacy_template = (root / "web" / "templates" / "privacy.html").read_text()
    heading = re.search(r"<h1>([^<]+)</h1>", privacy_template)

    assert heading is not None
    assert re.fullmatch(privacy_screen.heading, heading.group(1))


def test_public_screen_data_reuses_warmed_payload(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "public-screen-cache.db")
    monkeypatch.setattr(web_main, "shared_cache_get", lambda _name: None)
    monkeypatch.setattr(web_main, "shared_cache_set", lambda *_args: None)
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    web_main.PUBLIC_SCREEN_DATA_REFRESHING.clear()
    calls = 0

    def build() -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"value": "ready"}

    first = web_main._public_screen_data("test", "fixture", build)
    second = web_main._public_screen_data("test", "fixture", build)

    assert first == {"value": "ready"}
    assert second == first
    assert calls == 1
