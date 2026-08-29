from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from runner_node.api import NodeService, create_node_router
from runner_node.config import NodeSettings


def create_app(
    settings: NodeSettings | None = None,
    service: NodeService | None = None,
) -> FastAPI:
    settings = settings or (service.settings if service else NodeSettings.from_environment())
    service = service or NodeService(settings=settings)
    application = FastAPI(
        title="RATi Scanner Node",
        version="0.1.0",
        docs_url="/docs",
        redoc_url=None,
    )
    application.add_middleware(GZipMiddleware, minimum_size=1_000, compresslevel=5)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    application.include_router(create_node_router(service))
    return application


app = create_app()


def main() -> None:
    uvicorn.run(
        "runner_node.app:app",
        host=os.getenv("RATI_NODE_HOST", "127.0.0.1"),
        port=int(os.getenv("RATI_NODE_PORT", "8787")),
        reload=False,
        access_log=False,
    )


if __name__ == "__main__":
    main()
