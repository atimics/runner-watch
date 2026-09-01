from __future__ import annotations

import hmac
import json
import os
import secrets
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from runner_node import API_VERSION, SCANNER_VERSION
from runner_node.cloud_source import RatiCloudSource, RemoteScannerSource, normalize_scanner_url
from runner_node.config import NodeSettings
from runner_node.credentials import CredentialVault, credential_vault
from runner_node.openrouter import OpenRouterConnections
from runner_node.research import OpenRouterResearch, ResearchRequest
from runner_node.scans import ScanRequest, ScanStore, run_scan
from runner_node.tickers import load_ticker_detail
from runner_watch.source_catalog import DEFAULT_SOURCE_POLICIES


class OpenRouterKeyInput(BaseModel):
    key: str = Field(min_length=24, max_length=512)


class ProviderKeyInput(BaseModel):
    key: str = Field(min_length=8, max_length=2_048)


class SourceEnabledInput(BaseModel):
    enabled: bool


class RemoteScannerInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=2_048)
    token: str = Field(default="", max_length=2_048)


MARKET_PROVIDER_IDS = frozenset({"massive", "fintel", "the-odds-api"})
REMOTE_SCANNERS_VAULT_KEY = "remote-scanners"
RATI_CLOUD_ENABLED_VAULT_KEY = "rati-cloud-enabled"


class NodeService:
    def __init__(
        self,
        settings: NodeSettings | None = None,
        vault: CredentialVault | None = None,
        openrouter: OpenRouterConnections | None = None,
        scans: ScanStore | None = None,
        research: OpenRouterResearch | None = None,
        cloud_source: RatiCloudSource | None = None,
        remote_source: RemoteScannerSource | None = None,
    ) -> None:
        self.settings = settings or NodeSettings.from_environment()
        self.vault = vault or credential_vault(self.settings.credential_backend)
        self.openrouter = openrouter or OpenRouterConnections(self.vault)
        self.scans = scans or ScanStore(database_path=self.settings.database_path)
        self.research = research or OpenRouterResearch()
        self.cloud_source = cloud_source or RatiCloudSource()
        self.remote_source = remote_source or RemoteScannerSource()

    def require_authorized(self, request: Request) -> None:
        expected = self.settings.auth_token
        client_host = request.client.host if request.client else ""
        if (
            expected is None
            and self.settings.mode == "local"
            and client_host
            in {
                "127.0.0.1",
                "::1",
            }
        ):
            return
        supplied = request.headers.get("Authorization", "")
        scheme, _, token = supplied.partition(" ")
        if (
            expected is None
            or scheme.lower() != "bearer"
            or not token
            or not hmac.compare_digest(token, expected)
        ):
            raise HTTPException(401, "A valid scanner access token is required")

    def provider_credentials(self) -> dict[str, str]:
        return {
            provider: value
            for provider in MARKET_PROVIDER_IDS
            if (value := self.vault.get(provider))
        }

    def callback_origin(self, request: Request) -> str:
        if self.settings.public_origin:
            origin = self.settings.public_origin
        else:
            origin = str(request.base_url).rstrip("/")
        parsed = urlparse(origin)
        if self.settings.mode == "local" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise HTTPException(400, "Local OpenRouter connections require a loopback callback")
        if parsed.scheme not in {"http", "https"}:
            raise HTTPException(400, "OpenRouter callback origin must use HTTP or HTTPS")
        return origin

    def rati_cloud_enabled(self) -> bool:
        return self.vault.get(RATI_CLOUD_ENABLED_VAULT_KEY) == "enabled"

    def remote_scanners(self) -> list[dict[str, str]]:
        raw = self.vault.get(REMOTE_SCANNERS_VAULT_KEY)
        if not raw:
            return []
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(values, list):
            return []
        return [
            value
            for value in values[:10]
            if isinstance(value, dict)
            and all(isinstance(value.get(key), str) for key in ("id", "name", "url", "token"))
        ]

    def save_remote_scanner(self, payload: RemoteScannerInput) -> dict[str, str]:
        scanners = self.remote_scanners()
        if len(scanners) >= 10:
            raise ValueError("Remove a remote scanner before adding another")
        scanner = {
            "id": secrets.token_urlsafe(9),
            "name": payload.name.strip(),
            "url": normalize_scanner_url(payload.url),
            "token": payload.token.strip(),
        }
        scanners.append(scanner)
        self.vault.set(REMOTE_SCANNERS_VAULT_KEY, json.dumps(scanners, separators=(",", ":")))
        return scanner

    def delete_remote_scanner(self, scanner_id: str) -> bool:
        scanners = self.remote_scanners()
        remaining = [scanner for scanner in scanners if scanner["id"] != scanner_id]
        if len(remaining) == len(scanners):
            return False
        if remaining:
            self.vault.set(
                REMOTE_SCANNERS_VAULT_KEY,
                json.dumps(remaining, separators=(",", ":")),
            )
        else:
            self.vault.delete(REMOTE_SCANNERS_VAULT_KEY)
        return True

    def source_scan_receipts(self) -> dict[str, Any]:
        receipts: list[dict[str, Any]] = []
        warnings: list[str] = []
        sources: list[tuple[str, str, list[dict[str, Any]]]] = []
        if self.rati_cloud_enabled():
            try:
                sources.append(("rati-cloud", "RATi Cloud", self.cloud_source.scans()))
            except RuntimeError as exc:
                warnings.append(f"RATi Cloud: {exc}")
        for scanner in self.remote_scanners():
            try:
                values = self.remote_source.scans(scanner["url"], scanner["token"])
                sources.append((f"remote:{scanner['id']}", scanner["name"], values))
            except (RuntimeError, ValueError) as exc:
                warnings.append(f"{scanner['name']}: {exc}")
        for source_id, source_name, values in sources:
            receipts.extend(
                {**receipt, "source_id": source_id, "source_name": source_name}
                for receipt in values
            )
        return {"receipts": receipts[:40], "warnings": warnings}

    def provider_rows(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[Any]] = defaultdict(list)
        for policy in DEFAULT_SOURCE_POLICIES:
            grouped[policy.source].append(policy)
        output: list[dict[str, Any]] = []
        for source, policies in sorted(grouped.items()):
            credential_names = {
                policy.credential_env for policy in policies if policy.credential_env
            }
            built_in_contact = credential_names == {"SEC_USER_AGENT"}
            configured = (
                built_in_contact
                or not credential_names
                or any(os.getenv(name, "").strip() for name in credential_names)
            )
            if source in MARKET_PROVIDER_IDS:
                configured = bool(self.vault.get(source))
            enabled = any(policy.enabled for policy in policies) or source in MARKET_PROVIDER_IDS
            reviews = {policy.review_status for policy in policies}
            if not enabled:
                state = "disabled"
            elif not configured:
                state = "needs_configuration"
            elif not credential_names:
                state = "connected"
            elif reviews == {"approved"}:
                state = "ready"
            elif "review_required" in reviews:
                state = "review_required"
            else:
                state = "experimental"
            output.append(
                {
                    "id": source,
                    "title": policies[0].owner,
                    "state": state,
                    "enabled": enabled,
                    "configured": configured,
                    "configuration_kind": ("none" if built_in_contact else "api_key")
                    if credential_names
                    else "none",
                    "feeds": [
                        {
                            "id": policy.feed,
                            "title": policy.title,
                            "schedule": policy.schedule,
                            "review_status": policy.review_status,
                            "terms_url": policy.terms_url,
                        }
                        for policy in policies
                    ],
                }
            )
        output.insert(
            0,
            {
                "id": "built-in-scanner",
                "title": "Built-in scanner",
                "state": "connected",
                "enabled": True,
                "configured": True,
                "configuration_kind": "none",
                "feeds": [
                    {
                        "id": "momentum_scans",
                        "title": "Momentum scans and local receipts",
                        "schedule": "on_demand",
                        "review_status": "approved",
                        "terms_url": None,
                    }
                ],
            },
        )
        cloud_connected = self.rati_cloud_enabled()
        output.append(
            {
                "id": "rati-cloud",
                "title": "RATi Cloud",
                "state": "connected" if cloud_connected else "disabled",
                "enabled": cloud_connected,
                "configured": True,
                "configuration_kind": "toggle",
                "feeds": [
                    {
                        "id": "cloud_scans",
                        "title": "Shared scanner receipts",
                        "schedule": "on_demand",
                        "review_status": "approved",
                        "terms_url": "https://rati.chat",
                    }
                ],
            }
        )
        for scanner in self.remote_scanners():
            output.append(
                {
                    "id": f"remote:{scanner['id']}",
                    "title": scanner["name"],
                    "state": "connected",
                    "enabled": True,
                    "configured": True,
                    "configuration_kind": "remote_scanner",
                    "feeds": [
                        {
                            "id": "remote_scans",
                            "title": f"Scanner receipts from {scanner['url']}",
                            "schedule": "on_demand",
                            "review_status": "user_configured",
                            "terms_url": None,
                        }
                    ],
                }
            )
        output.append(
            {
                "id": "openrouter",
                "title": "OpenRouter",
                "state": self.openrouter.status()["status"],
                "enabled": self.settings.allow_user_openrouter,
                "configured": self.openrouter.status()["status"] == "connected",
                "configuration_kind": "oauth_pkce",
                "feeds": [
                    {
                        "id": "ai_research",
                        "title": "User-funded AI research",
                        "schedule": "on_demand",
                        "review_status": "approved",
                        "terms_url": "https://openrouter.ai/terms",
                    }
                ],
            }
        )
        return output

    def node_payload(self) -> dict[str, Any]:
        openrouter = self.openrouter.status()
        return {
            "api_version": API_VERSION,
            "scanner_version": SCANNER_VERSION,
            "node_id": self.settings.node_id,
            "mode": self.settings.mode,
            "capabilities": {
                "stocks": "ready",
                "sports": "ready",
                "research": (
                    "ready" if openrouter["status"] == "connected" else "missing_connection"
                ),
                "community": "ready" if self.settings.mode == "cloud" else "disconnected",
                "background_service": "ready",
            },
            "links": {
                "providers": "/api/v1/providers",
                "scans": "/api/v1/scans",
                "tickers": "/api/v1/tickers/{ticker}",
                "research": "/api/v1/research",
                "openrouter": "/api/v1/connections/openrouter",
                "source_scans": "/api/v1/source-scans",
            },
        }


def create_node_router(service: NodeService | None = None) -> APIRouter:
    service = service or NodeService()
    router = APIRouter(prefix="/api/v1", tags=["RATi Node"])

    def authorized(request: Request) -> None:
        service.require_authorized(request)

    protected = [Depends(authorized)]

    @router.get("/node")
    def node() -> dict[str, Any]:
        return service.node_payload()

    @router.get("/providers")
    def providers() -> dict[str, Any]:
        return {"providers": service.provider_rows()}

    @router.get("/scans", dependencies=protected)
    def scans() -> dict[str, object]:
        return {"receipts": service.scans.list()}

    @router.get("/source-scans", dependencies=protected)
    async def source_scans() -> dict[str, Any]:
        return await run_in_threadpool(service.source_scan_receipts)

    @router.post("/scans", dependencies=protected)
    async def create_scan(payload: ScanRequest) -> dict[str, object]:
        if service.settings.mode == "cloud":
            raise HTTPException(403, "Cloud scans are scheduled by the managed scanner")
        result = await run_in_threadpool(run_scan, payload, service.provider_credentials())
        return service.scans.save(result)

    @router.get("/scans/{scan_id}", dependencies=protected)
    def scan(scan_id: str) -> dict[str, object]:
        result = service.scans.get(scan_id)
        if result is None:
            raise HTTPException(404, "Scan not found")
        return result

    @router.get("/tickers/{ticker}", dependencies=protected)
    async def ticker(ticker: str) -> dict[str, object]:
        if service.settings.mode == "cloud":
            raise HTTPException(403, "Use the RATi Cloud ticker page with a cloud scanner")
        try:
            return await run_in_threadpool(
                load_ticker_detail,
                ticker,
                service.provider_credentials(),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/connections/openrouter")
    def openrouter_status() -> dict[str, Any]:
        return service.openrouter.status()

    @router.post("/research", dependencies=protected)
    async def research(payload: ResearchRequest) -> dict[str, Any]:
        key = service.vault.get("openrouter")
        if not key:
            raise HTTPException(409, "Connect OpenRouter before starting research")
        try:
            return await run_in_threadpool(service.research.run, payload, key)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.post("/connections/openrouter/start", dependencies=protected)
    def start_openrouter(request: Request) -> dict[str, Any]:
        if not service.settings.allow_user_openrouter:
            raise HTTPException(403, "User OpenRouter connections are disabled on this node")
        try:
            return service.openrouter.begin(service.callback_origin(request))
        except RuntimeError as exc:
            raise HTTPException(429, str(exc)) from exc

    @router.get("/connections/openrouter/flows/{flow_id}", dependencies=protected)
    def openrouter_flow(flow_id: str) -> dict[str, Any]:
        status = service.openrouter.flow_status(flow_id)
        if status is None:
            raise HTTPException(404, "Connection flow not found")
        return status

    @router.get(
        "/connections/openrouter/callback/{flow_id}",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    def openrouter_callback(flow_id: str, code: str = "") -> HTMLResponse:
        try:
            service.openrouter.complete(flow_id, code)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        return HTMLResponse(
            "<!doctype html><title>RATi connected</title>"
            "<main><h1>OpenRouter connected</h1>"
            "<p>You can close this window and return to RATi.</p></main>"
        )

    @router.put("/connections/openrouter", dependencies=protected)
    def connect_openrouter(payload: OpenRouterKeyInput) -> dict[str, Any]:
        if not service.settings.allow_user_openrouter:
            raise HTTPException(403, "User OpenRouter connections are disabled on this node")
        try:
            return service.openrouter.connect_key(payload.key)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.delete("/connections/openrouter", dependencies=protected)
    def disconnect_openrouter() -> dict[str, Any]:
        return service.openrouter.disconnect()

    @router.put("/sources/rati-cloud", dependencies=protected)
    def toggle_rati_cloud(payload: SourceEnabledInput) -> dict[str, Any]:
        if payload.enabled:
            service.vault.set(RATI_CLOUD_ENABLED_VAULT_KEY, "enabled")
        else:
            service.vault.delete(RATI_CLOUD_ENABLED_VAULT_KEY)
        return {
            "source": "rati-cloud",
            "status": "connected" if payload.enabled else "disabled",
        }

    @router.post("/connections/scanners", dependencies=protected)
    def connect_scanner(payload: RemoteScannerInput) -> dict[str, Any]:
        try:
            scanner = service.save_remote_scanner(payload)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "id": scanner["id"],
            "name": scanner["name"],
            "url": scanner["url"],
            "status": "connected",
        }

    @router.delete("/connections/scanners/{scanner_id}", dependencies=protected)
    def disconnect_scanner(scanner_id: str) -> dict[str, Any]:
        if not service.delete_remote_scanner(scanner_id):
            raise HTTPException(404, "Remote scanner not found")
        return {"id": scanner_id, "status": "disconnected"}

    @router.put("/connections/{provider}", dependencies=protected)
    def connect_provider(provider: str, payload: ProviderKeyInput) -> dict[str, Any]:
        if provider not in MARKET_PROVIDER_IDS:
            raise HTTPException(404, "This source does not accept a user credential")
        try:
            service.vault.set(provider, payload.key.strip())
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"provider": provider, "status": "connected"}

    @router.delete("/connections/{provider}", dependencies=protected)
    def disconnect_provider(provider: str) -> dict[str, Any]:
        if provider not in MARKET_PROVIDER_IDS:
            raise HTTPException(404, "This source does not accept a user credential")
        removed = service.vault.delete(provider)
        return {
            "provider": provider,
            "status": "connected" if service.vault.get(provider) else "disconnected",
            "removed": removed,
        }

    return router
