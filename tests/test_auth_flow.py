import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch
from starlette.requests import Request

from runner_web import db
from runner_web import main as web_main
from runner_web.db import connection, init_db
from runner_web.main import (
    APP_ORIGIN,
    login_page,
    register_options,
    save_challenge,
    take_challenge,
)


def request(method: str, path: str) -> Request:
    result = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"origin", APP_ORIGIN.encode())],
            "client": ("127.0.0.42", 4210),
        }
    )
    result.state.csp_nonce = "test"
    return result


def test_new_user_flow_leads_with_passkey_creation() -> None:
    response = login_page(request("GET", "/login"), None)
    html = response.body.decode()

    create_position = html.index("Create passkey")
    login_position = html.index("Log in with passkey")
    assert "Create your passkey" in html
    assert create_position < login_position
    assert "No password" in html


def test_new_passkey_is_created_on_the_current_device(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "auth-flow.db")
    init_db()

    response = register_options(request("POST", "/api/auth/register/options"))
    options = json.loads(response.body)["options"]
    selection = options["authenticatorSelection"]

    assert selection["authenticatorAttachment"] == "platform"
    assert selection["residentKey"] == "required"
    assert selection["userVerification"] == "required"


def test_passkey_challenge_can_only_be_consumed_once(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "challenge.db")
    init_db()
    token = save_challenge("login", b"challenge")

    assert take_challenge(token, "login")["challenge"] == b"challenge"
    with pytest.raises(HTTPException, match="expired"):
        take_challenge(token, "login")

    with connection() as database:
        assert (
            database.execute("SELECT 1 FROM auth_challenges WHERE token=?", (token,)).fetchone()
            is None
        )


def test_required_rate_limit_key_fails_closed(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(web_main, "PROCESS_ROLE", "all")
    monkeypatch.setattr(web_main, "REQUIRE_RATE_LIMIT_HASH_KEY", True)
    monkeypatch.setattr(web_main, "RATE_LIMIT_HASH_KEY_VALUE", "")

    with pytest.raises(RuntimeError, match="RATE_LIMIT_HASH_KEY is required"):
        web_main.validate_runtime_configuration()

    monkeypatch.setattr(web_main, "RATE_LIMIT_HASH_KEY_VALUE", "shared-test-key")
    web_main.validate_runtime_configuration()


def test_rate_limit_key_accepts_long_operator_secrets() -> None:
    first = web_main._rate_limit_hash_key("a" * 128)
    second = web_main._rate_limit_hash_key("a" * 128)

    assert len(first) == 32
    assert first == second
