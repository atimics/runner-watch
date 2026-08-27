from pathlib import Path

import pytest
from starlette.requests import Request

from runner_web.request_security import request_client_ip, safe_next_path


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

    assert "target.origin !== location.origin" in template
    assert "const nextPath = safeNextPath(requestedNext);" in template
    assert "--forwarded-allow-ips" not in dockerfile
    assert "--forwarded-allow-ips" not in fly_config
