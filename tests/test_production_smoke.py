from __future__ import annotations

import asyncio
import re
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse
from starlette.requests import Request

from runner_web import main as web_main
from runner_web.main import app


def _route_paths(routes: list[object]) -> set[str]:
    paths = {str(route.path) for route in routes if hasattr(route, "path")}
    for route in routes:
        included_router = getattr(route, "original_router", None)
        if included_router is not None:
            paths.update(_route_paths(included_router.routes))
    return paths


def test_production_smoke_only_checks_real_routes() -> None:
    script = (Path(__file__).parents[1] / "scripts/smoke-production").read_text()
    checked_endpoints = re.findall(r"^check_endpoint ['\"]?([^'\"\s]+)", script, re.MULTILINE)
    app_routes = _route_paths(app.routes)

    assert checked_endpoints
    assert all(urlsplit(endpoint).path in app_routes for endpoint in checked_endpoints)
    assert "/api/version" in checked_endpoints
    assert "EXPECTED_BUILD_SHA" in script


def test_version_endpoint_identifies_code_and_assets() -> None:
    assert web_main.version_api() == {
        "version": "0.1.0",
        "build_sha": web_main.APP_BUILD_SHA,
        "static_version": web_main.STATIC_VERSION,
    }

    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
    workflow = (Path(__file__).parents[1] / ".github/workflows/fly.yml").read_text()
    assert "ARG APP_BUILD_SHA=dev" in dockerfile
    assert '--build-arg APP_BUILD_SHA="${{ github.sha }}"' in workflow


def test_every_response_identifies_its_build() -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/live",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1000),
        }
    )

    async def call_next(_request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    response = asyncio.run(web_main.security_headers(request, call_next))

    assert response.headers["X-RATi-Build"] == web_main.APP_BUILD_SHA
    assert response.headers["X-RATi-Assets"] == web_main.STATIC_VERSION


def test_security_bootstrap_uses_secret_stores_without_deploying_fly() -> None:
    script_path = Path(__file__).parents[1] / "scripts/configure-production-security"
    script = script_path.read_text()

    subprocess.run(["sh", "-n", str(script_path)], check=True)
    assert script_path.stat().st_mode & stat.S_IXUSR
    assert "openssl rand -hex 32" in script
    assert "wrangler secret put EDGE_PROXY_SECRET" in script
    assert "flyctl secrets import" in script
    assert "--stage" in script
    assert "gh secret set OPERATIONS_TOKEN" in script
    assert "flyctl deploy" not in script
    assert "wrangler deploy" not in script
    assert "Cloudflare already has EDGE_PROXY_SECRET" in script
    assert "--keychain" in script
    assert "security add-generic-password" in script
    assert "security find-generic-password" in script


def test_deploy_health_check_uses_operations_token() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/fly.yml").read_text()

    assert 'Authorization: Bearer ${OPERATIONS_TOKEN}' in workflow
    assert "OPERATIONS_TOKEN: ${{ secrets.OPERATIONS_TOKEN }}" in workflow
