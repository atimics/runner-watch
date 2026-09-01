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
from runner_watch.source_capabilities import (
    CAPABILITY_BY_ID,
    SOURCE_CAPABILITIES,
    access_model_for_policy,
    capability_for_policy,
    usage_rights_for_policy,
)
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


class ProviderRouteInput(BaseModel):
    providers: list[str] = Field(min_length=1, max_length=20)


MARKET_PROVIDER_IDS = frozenset({"massive", "fintel", "the-odds-api"})
REMOTE_SCANNERS_VAULT_KEY = "remote-scanners"
RATI_CLOUD_ENABLED_VAULT_KEY = "rati-cloud-enabled"
PROVIDER_ROUTES_VAULT_KEY = "provider-routes"


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

    def provider_routes(self) -> dict[str, list[str]]:
        routes: dict[str, list[str]] = {capability.id: [] for capability in SOURCE_CAPABILITIES}
        for policy in DEFAULT_SOURCE_POLICIES:
            capability = capability_for_policy(policy)
            if policy.source not in routes[capability.id]:
                routes[capability.id].append(policy.source)

        # Preserve the scanner's existing preference for Massive daily bars
        # when a user has connected it. Yahoo remains the local fallback.
        routes["market_bars"] = [
            source for source in ("massive", "yahoo") if source in routes["market_bars"]
        ]

        raw = self.vault.get(PROVIDER_ROUTES_VAULT_KEY)
        if not raw:
            return routes
        try:
            saved = json.loads(raw)
        except (TypeError, ValueError):
            return routes
        if not isinstance(saved, dict):
            return routes
        for capability_id, providers in saved.items():
            if capability_id not in routes or not isinstance(providers, list):
                continue
            supported = routes[capability_id]
            requested = [
                provider
                for value in providers
                if isinstance(value, str)
                and (provider := value.strip().lower()) in supported
            ]
            routes[capability_id] = list(dict.fromkeys([*requested, *supported]))
        return routes

    def save_provider_route(self, capability_id: str, providers: list[str]) -> list[str]:
        routes = self.provider_routes()
        if capability_id not in CAPABILITY_BY_ID:
            raise ValueError("Unknown scanner capability")
        supported = routes[capability_id]
        requested = list(
            dict.fromkeys(provider.strip().lower() for provider in providers if provider.strip())
        )
        invalid = [provider for provider in requested if provider not in supported]
        if invalid:
            raise ValueError(
                f"{', '.join(invalid)} cannot provide {CAPABILITY_BY_ID[capability_id].title}"
            )
        if not requested:
            raise ValueError("Choose at least one provider")
        next_route = [
            *requested,
            *(provider for provider in supported if provider not in requested),
        ]
        raw = self.vault.get(PROVIDER_ROUTES_VAULT_KEY)
        try:
            saved = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        saved[capability_id] = next_route
        self.vault.set(PROVIDER_ROUTES_VAULT_KEY, json.dumps(saved, separators=(",", ":")))
        return next_route

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
            feeds = [
                {
                    "id": policy.feed,
                    "title": policy.title,
                    "schedule": policy.schedule,
                    "review_status": policy.review_status,
                    "terms_url": policy.terms_url,
                    "capabilities": [capability_for_policy(policy).id],
                    "usage_rights": list(usage_rights_for_policy(policy)),
                    "access_model": access_model_for_policy(policy),
                    "storage_policy": policy.storage_policy,
                    "display_policy": policy.display_policy,
                    "product": policy.product,
                }
                for policy in policies
            ]
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
                    "capabilities": sorted(
                        {capability for feed in feeds for capability in feed["capabilities"]}
                    ),
                    "feeds": feeds,
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
                "capabilities": ["scanner_results"],
                "feeds": [
                    {
                        "id": "momentum_scans",
                        "title": "Momentum scans and local receipts",
                        "schedule": "on_demand",
                        "review_status": "approved",
                        "terms_url": None,
                        "capabilities": ["scanner_results"],
                        "usage_rights": ["local_private", "store_normalized"],
                        "access_model": "included",
                        "storage_policy": "local_receipts",
                        "display_policy": "local_only",
                        "product": "runners",
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
                "capabilities": ["scanner_results"],
                "feeds": [
                    {
                        "id": "cloud_scans",
                        "title": "Shared scanner receipts",
                        "schedule": "on_demand",
                        "review_status": "approved",
                        "terms_url": "https://rati.chat",
                        "capabilities": ["scanner_results"],
                        "usage_rights": ["local_private", "store_normalized"],
                        "access_model": "included",
                        "storage_policy": "local_receipts",
                        "display_policy": "local_only",
                        "product": "runners",
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
                    "capabilities": ["scanner_results"],
                    "feeds": [
                        {
                            "id": "remote_scans",
                            "title": f"Scanner receipts from {scanner['url']}",
                            "schedule": "on_demand",
                            "review_status": "user_configured",
                            "terms_url": None,
                            "capabilities": ["scanner_results"],
                            "usage_rights": ["local_private", "store_normalized"],
                            "access_model": "user_managed",
                            "storage_policy": "local_receipts",
                            "display_policy": "local_only",
                            "product": "runners",
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
                "capabilities": ["ai_research"],
                "feeds": [
                    {
                        "id": "ai_research",
                        "title": "User-funded AI research",
                        "schedule": "on_demand",
                        "review_status": "approved",
                        "terms_url": "https://openrouter.ai/terms",
                        "capabilities": ["ai_research"],
                        "usage_rights": ["local_private", "store_normalized"],
                        "access_model": "bring_your_own",
                        "storage_policy": "user_selected_results",
                        "display_policy": "local_only",
                        "product": "runners",
                    }
                ],
            }
        )
        return output

    def coverage_payload(self) -> dict[str, Any]:
        providers = self.provider_rows()
        routes = self.provider_routes()
        rows: list[dict[str, Any]] = []
        for capability in SOURCE_CAPABILITIES:
            options: list[dict[str, Any]] = []
            for provider in providers:
                matching_feeds = [
                    feed
                    for feed in provider["feeds"]
                    if capability.id in feed.get("capabilities", [])
                ]
                if not matching_feeds:
                    continue
                rights = sorted(
                    {
                        right
                        for feed in matching_feeds
                        for right in feed.get("usage_rights", [])
                    }
                )
                reviews = {feed["review_status"] for feed in matching_feeds}
                review_status = (
                    "approved"
                    if reviews == {"approved"}
                    else "review_required"
                    if "review_required" in reviews
                    else "experimental"
                )
                options.append(
                    {
                        "provider_id": provider["id"],
                        "provider_title": provider["title"],
                        "state": provider["state"],
                        "enabled": provider["enabled"],
                        "configured": provider["configured"],
                        "configuration_kind": provider["configuration_kind"],
                        "review_status": review_status,
                        "usage_rights": rights,
                        "access_model": matching_feeds[0]["access_model"],
                        "feeds": [feed["title"] for feed in matching_feeds],
                        "terms_url": next(
                            (feed["terms_url"] for feed in matching_feeds if feed["terms_url"]),
                            None,
                        ),
                    }
                )

            route = routes.get(capability.id, [])
            effective_route = [
                provider_id
                for provider_id in route
                if any(
                    option["provider_id"] == provider_id
                    and option["enabled"]
                    and option["configured"]
                    for option in options
                )
            ]
            private_ready = any(option["enabled"] and option["configured"] for option in options)
            public_ready = any(
                option["enabled"]
                and option["configured"]
                and (
                    "public_display" in option["usage_rights"]
                    or "public_derived_signals" in option["usage_rights"]
                )
                for option in options
            )
            rows.append(
                {
                    "id": capability.id,
                    "title": capability.title,
                    "description": capability.description,
                    "core": capability.core,
                    "private_ready": private_ready,
                    "public_ready": public_ready,
                    "selected_provider": effective_route[0] if effective_route else None,
                    "provider_route": route,
                    "providers": options,
                }
            )
        return {
            "summary": {
                "private_ready": sum(row["private_ready"] for row in rows),
                "public_ready": sum(row["public_ready"] for row in rows),
                "total": len(rows),
                "core_private_ready": all(row["private_ready"] for row in rows if row["core"]),
                "core_public_ready": all(row["public_ready"] for row in rows if row["core"]),
            },
            "capabilities": rows,
        }

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
                "coverage": "/api/v1/coverage",
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

    @router.get("/coverage")
    def coverage() -> dict[str, Any]:
        return service.coverage_payload()

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
        result = await run_in_threadpool(
            run_scan,
            payload,
            service.provider_credentials(),
            service.provider_routes(),
        )
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
                service.provider_routes(),
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

    @router.put("/routes/{capability_id}", dependencies=protected)
    def set_provider_route(
        capability_id: str,
        payload: ProviderRouteInput,
    ) -> dict[str, Any]:
        try:
            providers = service.save_provider_route(capability_id, payload.providers)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"capability": capability_id, "providers": providers}

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
