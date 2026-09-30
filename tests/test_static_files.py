from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.testclient import TestClient

from runner_web.static_files import NoSourceMapStaticFiles

ROOT = Path(__file__).parents[1]


def test_a_source_map_is_never_served_even_when_one_exists(tmp_path: Path) -> None:
    (tmp_path / "app.js").write_text("console.log(1);\n")
    (tmp_path / "app.js.map").write_text('{"version":3,"sources":["secret.ts"]}')
    (tmp_path / "STYLE.CSS.MAP").write_text("{}")
    app = Starlette()
    app.mount("/static", NoSourceMapStaticFiles(directory=str(tmp_path)), name="static")
    client = TestClient(app)

    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.js.map").status_code == 404
    assert client.get("/static/STYLE.CSS.MAP").status_code == 404


def test_the_apps_static_mounts_refuse_source_maps() -> None:
    from runner_web.main import app

    mounts = {
        route.name: route.app
        for route in app.routes
        if getattr(route, "name", "") in {"static", "desktop"}
    }
    assert isinstance(mounts["static"], NoSourceMapStaticFiles)
    if "desktop" in mounts:
        assert isinstance(mounts["desktop"], NoSourceMapStaticFiles)


def test_no_shipped_static_file_points_at_a_source_map() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "web" / "static").rglob("*")
        if path.is_file()
        and (
            path.suffix == ".map"
            or (
                path.suffix in {".js", ".css", ".mjs"}
                and "sourceMappingURL" in path.read_text(errors="ignore")
            )
        )
    ]
    assert offenders == []


def test_the_desktop_renderer_build_is_configured_without_source_maps() -> None:
    config = (ROOT / "desktop" / "vite.config.ts").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "sourcemap: false" in config
    assert "find dist -name '*.map'" in dockerfile
