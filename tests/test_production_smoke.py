from __future__ import annotations

import asyncio
import re
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse
from starlette.requests import Request

from runner_watch import __version__
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
        "version": __version__,
        "build_sha": web_main.APP_BUILD_SHA,
        "static_version": web_main.STATIC_VERSION,
    }

    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
    workflow = (Path(__file__).parents[1] / ".github/workflows/fly.yml").read_text()
    assert "ARG APP_BUILD_SHA=dev" in dockerfile
    assert "APP_BUILD_SHA=${{ github.sha }}" in workflow


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
    assert "gh secret set" not in script
    assert "flyctl deploy" not in script
    assert "wrangler deploy" not in script
    assert "Cloudflare already has EDGE_PROXY_SECRET" in script
    assert "--keychain" in script
    assert "security add-generic-password" in script
    assert "security find-generic-password" in script


def test_deploy_health_check_keeps_the_operations_token_inside_fly() -> None:
    workflow = (Path(__file__).parents[1] / ".github/workflows/fly.yml").read_text()

    assert "flyctl ssh console" in workflow
    assert "python -m runner_web.deployment_check" in workflow
    assert "OPERATIONS_TOKEN: ${{ secrets.OPERATIONS_TOKEN }}" not in workflow


def test_parallel_deploy_gate_requires_every_job_to_succeed() -> None:
    import os

    import yaml

    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/fly.yml").read_text()
    )
    jobs = workflow["jobs"]
    gate = jobs["test"]
    always_required = ["lint", "unit", "browser", "evidence", "container"]
    assert set(gate["needs"]) == {*always_required, "coverage"}
    assert gate["if"] == "always()"
    assert jobs["deploy"]["needs"] == "test"
    assert "needs" not in jobs["lint"] and "needs" not in jobs["container"]
    # Evidence merges the test reports, so it waits for them but nothing else.
    assert jobs["evidence"]["needs"] == ["unit", "browser"]
    # Coverage is the expensive gate, so pull requests skip it and main must pass it.
    assert jobs["coverage"]["if"] == "github.event_name != 'pull_request'"
    command = gate["steps"][0]["run"]

    def gate_result(values: dict[str, str]) -> int:
        return subprocess.run(
            ["bash", "-e", "-c", command], env={**os.environ, **values}, check=False
        ).returncode

    pull_request = {
        **{f"{name.upper()}_RESULT": "success" for name in always_required},
        "COVERAGE_RESULT": "skipped",
        "EVENT_NAME": "pull_request",
    }
    assert gate_result(pull_request) == 0
    main = {**pull_request, "COVERAGE_RESULT": "success", "EVENT_NAME": "push"}
    assert gate_result(main) == 0
    # A skipped coverage job must not satisfy the deploy gate on main.
    assert gate_result({**main, "COVERAGE_RESULT": "skipped"}) != 0
    for name in always_required:
        failing = {**main, f"{name.upper()}_RESULT": "failure"}
        assert gate_result(failing) != 0, name


def test_build_identity_is_set_after_reusable_image_layers() -> None:
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
    base, runtime = dockerfile.split("FROM base AS runtime", 1)
    assert "APP_BUILD_SHA" not in base
    assert "ARG APP_BUILD_SHA=dev" in runtime
    assert "ENV APP_BUILD_SHA=${APP_BUILD_SHA}" in runtime
