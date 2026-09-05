from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

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


def _watch_parent_input(server: uvicorn.Server) -> None:
    # The desktop owns the write end of this pipe, including through PyInstaller.
    while sys.stdin.read(1):
        pass
    server.should_exit = True
    time.sleep(5)
    os._exit(0)


def main() -> None:
    host = os.getenv("RATI_NODE_HOST", "127.0.0.1")
    port = int(os.getenv("RATI_NODE_PORT", "8787"))
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(2_048)
    actual_port = int(listener.getsockname()[1])
    print(
        json.dumps({"event": "ready", "url": f"http://{host}:{actual_port}"}),
        flush=True,
    )
    config = uvicorn.Config(
        "runner_node.app:app",
        host=host,
        port=actual_port,
        reload=False,
        access_log=False,
    )
    server = uvicorn.Server(config)
    if os.getenv("RATI_NODE_EXIT_ON_STDIN_CLOSE") == "1":
        threading.Thread(
            target=_watch_parent_input,
            args=(server,),
            daemon=True,
            name="desktop-parent",
        ).start()
    server.run(sockets=[listener])


if __name__ == "__main__":
    main()
