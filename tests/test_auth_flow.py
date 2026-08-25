import json
from pathlib import Path

from pytest import MonkeyPatch
from starlette.requests import Request

from runner_web import db
from runner_web.db import init_db
from runner_web.main import APP_ORIGIN, login_page, register_options


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
    login_position = html.index("Use a passkey to log in")
    assert "Create your passkey" in html
    assert create_position < login_position
    assert "No username or password" in html


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
