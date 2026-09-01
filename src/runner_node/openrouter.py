from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_node.credentials import PROVIDER_ENVIRONMENTS, CredentialVault

AUTH_URL = "https://openrouter.ai/auth"
EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
FLOW_LIFETIME = timedelta(minutes=10)
FLOW_STATUS_LIFETIME = timedelta(minutes=15)
FLOW_START_WINDOW = timedelta(minutes=1)
MAX_PENDING_FLOWS = 20
MAX_FLOW_STATUSES = 100
MAX_FLOW_STARTS_PER_WINDOW = 12

ExchangeCode = Callable[[str, str], dict[str, Any]]


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _exchange_code(code: str, verifier: str) -> dict[str, Any]:
    body = json.dumps(
        {
            "code": code,
            "code_verifier": verifier,
            "code_challenge_method": "S256",
        }
    ).encode()
    request = urllib.request.Request(
        EXCHANGE_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"OpenRouter authorization failed ({exc.code}): {detail}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError("OpenRouter authorization could not be completed") from exc


@dataclass(frozen=True, slots=True)
class PendingFlow:
    id: str
    verifier: str
    callback_url: str
    expires_at: datetime


class OpenRouterConnections:
    """Own OpenRouter PKCE state and keep credentials outside the renderer."""

    def __init__(
        self,
        vault: CredentialVault,
        exchange_code: ExchangeCode = _exchange_code,
    ) -> None:
        self.vault = vault
        self.exchange_code = exchange_code
        self._flows: dict[str, PendingFlow] = {}
        self._flow_status: dict[str, dict[str, Any]] = {}
        self._flow_status_expires: dict[str, datetime] = {}
        self._flow_starts: deque[datetime] = deque()
        self._lock = threading.Lock()

    def _purge(self) -> None:
        current = datetime.now(UTC)
        while self._flow_starts and self._flow_starts[0] <= current - FLOW_START_WINDOW:
            self._flow_starts.popleft()
        stale_statuses = [
            flow_id
            for flow_id, expires_at in self._flow_status_expires.items()
            if expires_at <= current
        ]
        for flow_id in stale_statuses:
            self._flow_status.pop(flow_id, None)
            self._flow_status_expires.pop(flow_id, None)
        expired = [flow_id for flow_id, flow in self._flows.items() if flow.expires_at <= current]
        for flow_id in expired:
            self._flows.pop(flow_id, None)
            self._flow_status[flow_id] = {"status": "expired"}
            self._flow_status_expires[flow_id] = current + FLOW_STATUS_LIFETIME
        self._trim_statuses()

    def _trim_statuses(self) -> None:
        while len(self._flow_status) > MAX_FLOW_STATUSES:
            flow_id = next(iter(self._flow_status))
            self._flow_status.pop(flow_id, None)
            self._flow_status_expires.pop(flow_id, None)

    def begin(self, callback_origin: str) -> dict[str, Any]:
        flow_id = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        challenge = _base64url(hashlib.sha256(verifier.encode()).digest())
        callback_url = f"{callback_origin}/api/v1/connections/openrouter/callback/{flow_id}"
        expires_at = datetime.now(UTC) + FLOW_LIFETIME
        flow = PendingFlow(flow_id, verifier, callback_url, expires_at)
        with self._lock:
            self._purge()
            if len(self._flow_starts) >= MAX_FLOW_STARTS_PER_WINDOW:
                raise RuntimeError("Too many OpenRouter connection attempts; wait a minute")
            self._flow_starts.append(datetime.now(UTC))
            while len(self._flows) >= MAX_PENDING_FLOWS:
                oldest = next(iter(self._flows))
                self._flows.pop(oldest, None)
                self._flow_status[oldest] = {"status": "expired"}
                self._flow_status_expires[oldest] = datetime.now(UTC) + FLOW_STATUS_LIFETIME
            self._flows[flow_id] = flow
            self._flow_status[flow_id] = {"status": "pending"}
            self._flow_status_expires[flow_id] = expires_at + FLOW_STATUS_LIFETIME
            self._trim_statuses()
        query = urllib.parse.urlencode(
            {
                "callback_url": callback_url,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "key_label": "RATi Desktop",
            }
        )
        return {
            "flow_id": flow_id,
            "authorization_url": f"{AUTH_URL}?{query}",
            "expires_at": expires_at.isoformat(),
        }

    def flow_status(self, flow_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._purge()
            status = self._flow_status.get(flow_id)
            return dict(status) if status else None

    def complete(self, flow_id: str, code: str) -> dict[str, Any]:
        with self._lock:
            self._purge()
            flow = self._flows.pop(flow_id, None)
        if flow is None:
            raise ValueError("This OpenRouter connection request expired or was already used")
        if not code.strip():
            raise ValueError("OpenRouter did not return an authorization code")
        try:
            result = self.exchange_code(code.strip(), flow.verifier)
            key = str(result.get("key") or "").strip()
            if not key:
                raise RuntimeError("OpenRouter did not return an API key")
            self.vault.set("openrouter", key)
        except Exception as exc:
            with self._lock:
                self._flow_status[flow_id] = {"status": "failed", "detail": str(exc)[:240]}
                self._flow_status_expires[flow_id] = datetime.now(UTC) + FLOW_STATUS_LIFETIME
                self._trim_statuses()
            raise
        status = {"status": "connected", "connection": self.status()}
        with self._lock:
            self._flow_status[flow_id] = status
            self._flow_status_expires[flow_id] = datetime.now(UTC) + FLOW_STATUS_LIFETIME
            self._trim_statuses()
        return status

    def connect_key(self, key: str) -> dict[str, Any]:
        clean = key.strip()
        if not clean.startswith("sk-or-") or len(clean) < 24:
            raise ValueError("Enter a valid OpenRouter API key")
        self.vault.set("openrouter", clean)
        return self.status()

    def status(self) -> dict[str, Any]:
        key = self.vault.get("openrouter")
        environment_name = PROVIDER_ENVIRONMENTS["openrouter"]
        environment_managed = bool(__import__("os").getenv(environment_name, "").strip())
        if not key:
            return {"status": "disconnected", "provider": "openrouter"}
        return {
            "status": "connected",
            "provider": "openrouter",
            "credential_owner": "operator" if environment_managed else "user",
            "connection_method": "environment" if environment_managed else "stored",
            "activity_url": "https://openrouter.ai/activity",
            "settings_url": "https://openrouter.ai/settings/keys",
        }

    def disconnect(self) -> dict[str, Any]:
        removed = self.vault.delete("openrouter")
        status = self.status()
        return {**status, "removed": removed}
