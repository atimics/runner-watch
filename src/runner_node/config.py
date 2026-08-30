from __future__ import annotations

import os
import socket
from dataclasses import dataclass


def _csv(name: str, default: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, default).split(",") if value.strip())


@dataclass(frozen=True, slots=True)
class NodeSettings:
    """Runtime settings shared by local, self-hosted, and cloud scanner nodes."""

    mode: str
    node_id: str
    public_origin: str | None
    allowed_origins: tuple[str, ...]
    allow_user_openrouter: bool
    credential_backend: str
    auth_token: str | None = None
    database_path: str | None = None

    @classmethod
    def from_environment(cls) -> NodeSettings:
        inferred_mode = "cloud" if os.getenv("DATABASE_URL", "").strip() else "local"
        mode = os.getenv("RATI_NODE_MODE", inferred_mode).strip().lower()
        if mode not in {"local", "self_hosted", "cloud"}:
            raise ValueError("RATI_NODE_MODE must be local, self_hosted, or cloud")
        public_origin = (
            os.getenv("RATI_NODE_PUBLIC_ORIGIN", "").strip()
            or os.getenv("APP_ORIGIN", "").strip()
            or None
        )
        allow_default = "false" if mode == "cloud" else "true"
        allow_user_openrouter = os.getenv(
            "RATI_ALLOW_USER_OPENROUTER", allow_default
        ).strip().lower() not in {"0", "false", "no", "off"}
        backend_default = "environment" if mode == "cloud" else "keyring"
        auth_token = os.getenv("RATI_NODE_TOKEN", "").strip() or None
        if mode == "self_hosted" and (auth_token is None or len(auth_token) < 24):
            raise ValueError(
                "RATI_NODE_TOKEN must contain at least 24 characters in self_hosted mode"
            )
        return cls(
            mode=mode,
            node_id=os.getenv("RATI_NODE_ID", f"{mode}-{socket.gethostname()}").strip(),
            public_origin=public_origin.rstrip("/") if public_origin else None,
            allowed_origins=_csv(
                "RATI_NODE_ALLOWED_ORIGINS",
                "rati-app://app,http://127.0.0.1:5173,http://localhost:5173",
            ),
            allow_user_openrouter=allow_user_openrouter,
            credential_backend=os.getenv("RATI_CREDENTIAL_BACKEND", backend_default)
            .strip()
            .lower(),
            auth_token=auth_token,
            database_path=os.getenv("DATABASE_PATH", "").strip() or None,
        )
