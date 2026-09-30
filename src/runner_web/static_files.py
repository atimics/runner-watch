"""Static files that never serve a source map."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope


class NoSourceMapStaticFiles(StaticFiles):
    """Serve static files, but answer 404 for any source map.

    Nothing in this repo builds source maps for the public site. This keeps it that
    way if a build tool is ever configured to emit them.
    """

    async def get_response(self, path: str, scope: Scope):  # type: ignore[override]
        if path.lower().endswith(".map"):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)
