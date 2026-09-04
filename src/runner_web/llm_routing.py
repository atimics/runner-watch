from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

RoutingPolicy = Literal["managed", "prefer_customer", "customer_only"]
RouteKind = Literal["managed", "edge"]

ROUTING_POLICIES = {"managed", "prefer_customer", "customer_only"}
ROUTE_KINDS = {"managed", "edge"}
MAX_PROVIDER_RESPONSE_BYTES = 8 * 1024 * 1024
EDGE_CONNECTOR_ONLINE_SECONDS = 120


@dataclass(frozen=True, slots=True)
class InferenceRoute:
    policy: RoutingPolicy
    kind: RouteKind
    model: str
    connector_id: str | None = None
    customer_inference: bool = False
    available: bool = True
    unavailable_reason: str | None = None

    def snapshot(self) -> dict[str, Any]:

        return {
            "policy": self.policy,
            "kind": self.kind,
            "model": self.model,
            "connector_id": self.connector_id,
            "customer_inference": self.customer_inference,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "model_identity": "self_reported" if self.customer_inference else "managed",
        }


class LLMRouteError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 502,
        diagnostics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.diagnostics = diagnostics or {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def connector_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _global_addresses(hostname: str) -> list[str]:
    try:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LLMRouteError(
            "The model endpoint hostname could not be resolved.",
            status_code=400,
            diagnostics={"phase": "endpoint_resolution"},
        ) from exc
    addresses = sorted({str(record[4][0]).split("%", 1)[0] for record in records})
    if not addresses:
        raise LLMRouteError(
            "The model endpoint hostname has no address.",
            status_code=400,
            diagnostics={"phase": "endpoint_resolution"},
        )
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise LLMRouteError(
                "The model endpoint resolved to an invalid address.",
                status_code=400,
                diagnostics={"phase": "endpoint_resolution"},
            ) from exc
        if not parsed.is_global:
            raise LLMRouteError(
                "Cloud model endpoints must use a public internet address.",
                status_code=400,
                diagnostics={"phase": "endpoint_resolution"},
            )
    return addresses


def normalize_openai_base_url(value: str, *, allow_local: bool = False) -> str:

    candidate = value.strip().rstrip("/")
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise LLMRouteError("The model endpoint URL is invalid.", status_code=400) from exc
    allowed_schemes = {"http", "https"} if allow_local else {"https"}
    if parsed.scheme.lower() not in allowed_schemes:
        raise LLMRouteError(
            "Cloud model endpoints must use HTTPS.",
            status_code=400,
            diagnostics={"phase": "endpoint_validation"},
        )
    if not parsed.hostname or parsed.username or parsed.password:
        raise LLMRouteError(
            "The model endpoint URL is invalid.",
            status_code=400,
            diagnostics={"phase": "endpoint_validation"},
        )
    if parsed.query or parsed.fragment:
        raise LLMRouteError(
            "The model endpoint cannot contain a query or fragment.",
            status_code=400,
            diagnostics={"phase": "endpoint_validation"},
        )
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        raise LLMRouteError(
            "The model endpoint must end in /v1.",
            status_code=400,
            diagnostics={"phase": "endpoint_validation"},
        )
    if not allow_local:
        _global_addresses(parsed.hostname)
    elif parsed.scheme.lower() == "http":
        addresses = _global_or_local_addresses(parsed.hostname)
        if any(ipaddress.ip_address(address).is_global for address in addresses):
            raise LLMRouteError(
                "Public model endpoints must use HTTPS.",
                status_code=400,
                diagnostics={"phase": "endpoint_validation"},
            )
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme.lower(), host, path, "", ""))


def _global_or_local_addresses(hostname: str) -> list[str]:
    try:
        records = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise LLMRouteError(
            "The local model endpoint hostname could not be resolved.",
            status_code=400,
            diagnostics={"phase": "endpoint_resolution"},
        ) from exc
    addresses = sorted({str(record[4][0]).split("%", 1)[0] for record in records})
    if not addresses:
        raise LLMRouteError("The local model endpoint hostname has no address.", status_code=400)
    try:
        for address in addresses:
            ipaddress.ip_address(address)
    except ValueError as exc:
        raise LLMRouteError(
            "The local model endpoint address is invalid.", status_code=400
        ) from exc
    return addresses


def _request_json(
    url: str,
    *,
    method: str,
    api_key: str | None,
    body: dict[str, Any] | None,
    timeout: int,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, separators=(",", ":")).encode()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            declared_size = int(response.headers.get("Content-Length") or 0)
            if declared_size > MAX_PROVIDER_RESPONSE_BYTES:
                raise LLMRouteError(
                    "The model endpoint returned too much data.",
                    diagnostics={"phase": "provider_response_size"},
                )
            raw = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
            if len(raw) > MAX_PROVIDER_RESPONSE_BYTES:
                raise LLMRouteError(
                    "The model endpoint returned too much data.",
                    diagnostics={"phase": "provider_response_size"},
                )
        result = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            message = "The model endpoint tried to redirect the request."
        elif exc.code in {401, 403}:
            message = "The model endpoint rejected its API key."
        elif exc.code == 429:
            message = "The model endpoint is busy."
        else:
            message = "The model endpoint could not complete the request."
        raise LLMRouteError(
            message,
            status_code=exc.code if 400 <= exc.code < 500 else 502,
            diagnostics={"phase": "provider_http", "http_status": exc.code},
        ) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise LLMRouteError(
            "The model endpoint took too long to answer.",
            status_code=504,
            diagnostics={"phase": "provider_timeout"},
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise LLMRouteError(
            "The model endpoint returned an unreadable response.",
            diagnostics={"phase": "provider_envelope"},
        ) from exc
    if not isinstance(result, dict):
        raise LLMRouteError(
            "The model endpoint returned an unreadable response.",
            diagnostics={"phase": "provider_envelope"},
        )
    return result


def probe_openai_compatible(
    base_url: str,
    *,
    api_key: str | None = None,
    allow_local: bool = False,
    timeout: int = 10,
) -> dict[str, Any]:
    base = normalize_openai_base_url(base_url, allow_local=allow_local)
    result = _request_json(
        f"{base}/models",
        method="GET",
        api_key=api_key,
        body=None,
        timeout=timeout,
    )
    models = result.get("data")
    model_ids = (
        [str(item.get("id"))[:160] for item in models if isinstance(item, dict) and item.get("id")]
        if isinstance(models, list)
        else []
    )
    return {"ok": True, "models": model_ids[:100]}


def call_chat_completions(
    base_url: str,
    body: dict[str, Any],
    *,
    api_key: str | None = None,
    allow_local: bool = False,
    timeout: int = 300,
) -> dict[str, Any]:
    base = normalize_openai_base_url(base_url, allow_local=allow_local)
    return _request_json(
        f"{base}/chat/completions",
        method="POST",
        api_key=api_key,
        body=body,
        timeout=timeout,
    )


def route_for_user(database: Any, user_id: str, *, managed_model: str) -> InferenceRoute:
    row = database.execute(
        "SELECT * FROM user_llm_routes WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if not row or str(row["policy"]) == "managed":
        return InferenceRoute(policy="managed", kind="managed", model=managed_model)

    policy = str(row["policy"])
    kind = str(row["route_kind"])
    model = str(row["model"] or "").strip()
    connector_id = str(row["connector_id"] or "").strip() or None
    available = bool(model)
    reason = None if available else "Choose a model before using your model route."
    if kind == "edge":
        connector = (
            database.execute(
                "SELECT status,last_seen_at FROM llm_edge_connectors WHERE id=? AND user_id=?",
                (connector_id, user_id),
            ).fetchone()
            if connector_id
            else None
        )
        last_seen_at = None
        if connector and connector["last_seen_at"]:
            try:
                last_seen_at = datetime.fromisoformat(str(connector["last_seen_at"]))
                if last_seen_at.tzinfo is None:
                    last_seen_at = last_seen_at.replace(tzinfo=UTC)
            except ValueError:
                last_seen_at = None
        connector_online = bool(
            connector
            and str(connector["status"]) == "active"
            and last_seen_at
            and last_seen_at >= datetime.now(UTC) - timedelta(seconds=EDGE_CONNECTOR_ONLINE_SECONDS)
        )
        available = available and connector_online
        reason = None if available else "Start your local model connector."
    else:
        available = False
        reason = "The saved model route is invalid."

    if not available and policy == "prefer_customer":
        return InferenceRoute(policy="prefer_customer", kind="managed", model=managed_model)
    return InferenceRoute(
        policy=policy,
        kind=kind,
        model=model or managed_model,
        connector_id=connector_id,
        customer_inference=True,
        available=available,
        unavailable_reason=reason,
    )
