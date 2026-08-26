from __future__ import annotations

import re
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
