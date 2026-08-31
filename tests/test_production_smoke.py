from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

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


def test_security_bootstrap_uses_secret_stores_without_deploying_fly() -> None:
    script_path = Path(__file__).parents[1] / "scripts/configure-production-security"
    script = script_path.read_text()

    subprocess.run(["sh", "-n", str(script_path)], check=True)
    assert script_path.stat().st_mode & stat.S_IXUSR
    assert "openssl rand -hex 32" in script
    assert "wrangler secret put EDGE_PROXY_SECRET" in script
    assert "flyctl secrets import" in script
    assert "--stage" in script
    assert "flyctl deploy" not in script
    assert "wrangler deploy" not in script
    assert "Cloudflare already has EDGE_PROXY_SECRET" in script
