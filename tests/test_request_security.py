import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from runner_web import main as web_main
from runner_web.request_security import (
    edge_proxy_authenticated,
    request_client_ip,
    safe_next_path,
)


def test_client_ip_uses_fly_header_only_when_enabled() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/pulse",
            "headers": [
                (b"fly-client-ip", b"203.0.113.7"),
                (b"x-forwarded-for", b"198.51.100.99"),
            ],
            "client": ("172.16.0.5", 4200),
        }
    )

    assert request_client_ip(request, trust_fly_client_ip=False) == "172.16.0.5"
    assert request_client_ip(request, trust_fly_client_ip=True) == "203.0.113.7"


def test_invalid_fly_client_ip_falls_back_to_direct_peer() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [(b"fly-client-ip", b"not-an-ip")],
            "client": ("172.16.0.5", 4200),
        }
    )

    assert request_client_ip(request, trust_fly_client_ip=True) == "172.16.0.5"


def test_authenticated_edge_client_ip_takes_priority_over_proxy_ip() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"fly-client-ip", b"198.51.100.20"),
                (b"x-rati-client-ip", b"203.0.113.8"),
                (b"x-rati-edge-secret", b"edge-secret"),
            ],
            "client": ("172.16.0.5", 4200),
        }
    )

    assert edge_proxy_authenticated(request, edge_proxy_secret="edge-secret") is True
    assert (
        request_client_ip(
            request,
            trust_fly_client_ip=True,
            edge_proxy_secret="edge-secret",
        )
        == "203.0.113.8"
    )


def test_spoofed_edge_client_ip_is_ignored_without_the_shared_secret() -> None:
    request = Request(
        {
            "type": "http",
            "headers": [
                (b"fly-client-ip", b"198.51.100.20"),
                (b"x-rati-client-ip", b"203.0.113.8"),
                (b"x-rati-edge-secret", b"wrong-secret"),
            ],
            "client": ("172.16.0.5", 4200),
        }
    )

    assert edge_proxy_authenticated(request, edge_proxy_secret="edge-secret") is False
    assert (
        request_client_ip(
            request,
            trust_fly_client_ip=True,
            edge_proxy_secret="edge-secret",
        )
        == "198.51.100.20"
    )


@pytest.mark.parametrize(
    "value",
    ["https://evil.example/", "//evil.example/", "/\\evil.example/", "/\nevil"],
)
def test_auth_redirect_rejects_external_or_ambiguous_paths(value: str) -> None:
    assert safe_next_path(value) == "/"


def test_auth_redirect_preserves_a_local_path() -> None:
    assert safe_next_path("/t/PEN?tab=chart#latest") == "/t/PEN?tab=chart#latest"


def test_deployment_does_not_trust_forwarded_headers_from_every_peer() -> None:
    root = Path(__file__).parents[1]
    template = (root / "web/templates/auth.html").read_text()
    dockerfile = (root / "Dockerfile").read_text()
    fly_config = (root / "fly.toml").read_text()
    worker = (root / "cloudflare-router/src/index.js").read_text()
    worker_config = json.loads((root / "cloudflare-router/wrangler.jsonc").read_text())

    assert "target.origin !== location.origin" in template
    assert "const nextPath = safeNextPath(requestedNext);" in template
    assert "--forwarded-allow-ips" not in dockerfile
    assert "--forwarded-allow-ips" not in fly_config
    assert 'headers.set("X-Rati-Client-IP"' in worker
    assert 'headers.set("X-Rati-Edge-Secret"' in worker
    assert "env.EDGE_PROXY_SECRET" in worker
    assert worker_config["secrets"]["required"] == ["EDGE_PROXY_SECRET"]
    assert 'REQUIRE_EDGE_PROXY_SECRET = "1"' in fly_config
    assert 'REGISTRATION_MODE = "invite"' in fly_config


@pytest.mark.parametrize("card", ["signal", "research"])
def test_generated_image_cards_are_rate_limited(
    card: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[str] = []

    def reject(_request: Request, scope: str, **_kwargs: object) -> None:
        checked.append(scope)
        raise HTTPException(429, "Too many requests")

    monkeypatch.setattr(web_main, "enforce_rate", reject)
    test_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/{card}/example/card.png",
            "headers": [],
            "client": ("127.0.0.1", 4200),
        }
    )

    with pytest.raises(HTTPException) as limited:
        if card == "signal":
            web_main.signal_card("example", test_request)
        else:
            monkeypatch.setattr(
                web_main,
                "get_commission",
                lambda _public_id: {"visibility": "public", "user_id": "owner"},
            )
            web_main.research_report_card("example", test_request, None)

    assert limited.value.status_code == 429
    assert checked == [f"{card}-card"]
