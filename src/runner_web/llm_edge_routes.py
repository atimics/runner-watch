"""Local-model routing, edge connectors and edge report jobs.

A customer can point report generation at a model running on their own machine.
These routes manage the route policy and connector tokens, and serve the job
lease the desktop connector polls. The server leases one report job at a time
and accepts the finished response through the same contract as a managed model.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from runner_web.db import connection
from runner_web.llm_routing import connector_token_hash

LOG = logging.getLogger(__name__)


class LLMRoutePayload(BaseModel):
    policy: Literal["managed", "prefer_customer", "customer_only"]
    route_kind: Literal["managed", "edge"]
    model: str = Field(default="", max_length=160)
    connector_id: str | None = Field(default=None, max_length=80)


class EdgeConnectorPayload(BaseModel):
    name: str = Field(default="Local model", min_length=1, max_length=80)


class EdgeJobCompletePayload(BaseModel):
    response: dict[str, Any]


class EdgeJobFailPayload(BaseModel):
    error: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class LLMEdgeRouteDependencies:
    templates: Jinja2Templates
    page_context: Callable[..., dict[str, Any]]
    current_user: Callable[[str | None], dict[str, Any] | None]
    require_user: Callable[[str | None], dict[str, Any]]
    require_origin: Callable[[Request], None]
    enforce_rate: Callable[..., None]
    now: Callable[[], datetime]
    iso: Callable[..., str]
    json_container: Callable[[Any, Any], Any]
    run_research_commission: Callable[..., dict[str, Any]]
    commission_api_payload: Callable[..., dict[str, Any]]
    edge_job_lease_minutes: int


@dataclass(frozen=True)
class LLMEdgeRoutes:
    router: APIRouter
    model_settings_page: Callable[..., Response]
    account_llm_route_api: Callable[..., JSONResponse]
    update_account_llm_route_api: Callable[..., JSONResponse]
    create_llm_connector_api: Callable[..., JSONResponse]
    revoke_llm_connector_api: Callable[..., JSONResponse]
    claim_edge_job_api: Callable[..., JSONResponse]
    heartbeat_edge_job_api: Callable[..., JSONResponse]
    complete_edge_job_api: Callable[..., JSONResponse]
    fail_edge_job_api: Callable[..., JSONResponse]


def _llm_settings_data(user_id: str) -> dict[str, Any]:
    with connection() as database:
        row = database.execute(
            "SELECT * FROM user_llm_routes WHERE user_id=?",
            (user_id,),
        ).fetchone()
        connectors = database.execute(
            """
            SELECT id,name,status,last_seen_at,created_at,updated_at
            FROM llm_edge_connectors
            WHERE user_id=? ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()
    route = (
        {
            "policy": str(row["policy"]),
            "route_kind": str(row["route_kind"]),
            "model": str(row["model"] or ""),
            "connector_id": str(row["connector_id"] or "") or None,
            "last_error": str(row["last_error"] or "") or None,
        }
        if row
        else {
            "policy": "managed",
            "route_kind": "managed",
            "model": "",
            "connector_id": None,
            "last_error": None,
        }
    )
    return {"route": route, "connectors": [dict(connector) for connector in connectors]}


def create_llm_edge_routes(dependencies: LLMEdgeRouteDependencies) -> LLMEdgeRoutes:
    router = APIRouter()

    @router.get("/settings/models", response_class=HTMLResponse)
    def model_settings_page(
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> Response:
        user = dependencies.current_user(runner_session)
        if not user:
            return RedirectResponse("/login?next=/settings/models", status_code=303)
        return dependencies.templates.TemplateResponse(
            request=request,
            name="model_settings.html",
            context=dependencies.page_context(
                request,
                runner_session,
                llm_settings=_llm_settings_data(str(user["id"])),
            ),
        )

    @router.get("/api/account/llm-route")
    def account_llm_route_api(
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        user = dependencies.require_user(runner_session)
        dependencies.enforce_rate(
            request, "llm-route-read", limit=60, seconds=60, subject=user["id"]
        )
        response = JSONResponse(_llm_settings_data(str(user["id"])))
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.put("/api/account/llm-route")
    def update_account_llm_route_api(
        payload: LLMRoutePayload,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        user_id = str(user["id"])
        dependencies.enforce_rate(
            request, "llm-route-write", limit=20, seconds=3600, subject=user_id
        )
        model = payload.model.strip()
        connector_id = payload.connector_id if payload.route_kind == "edge" else None
        if payload.policy == "managed":
            if payload.route_kind != "managed":
                raise HTTPException(400, "Managed routing cannot use a local connector.")
            model = ""
        else:
            if payload.route_kind != "edge":
                raise HTTPException(400, "Choose a local connector for your model policy.")
            if not model:
                raise HTTPException(400, "Enter the model ID loaded by LM Studio or Unsloth.")
            if not connector_id:
                raise HTTPException(400, "Create and choose a local connector.")
        timestamp = dependencies.iso()
        with connection() as database:
            if connector_id:
                connector = database.execute(
                    """
                    SELECT id FROM llm_edge_connectors
                    WHERE id=? AND user_id=? AND status='active'
                    """,
                    (connector_id, user_id),
                ).fetchone()
                if not connector:
                    raise HTTPException(404, "Local connector not found.")
            database.execute(
                """
                INSERT INTO user_llm_routes(
                    user_id,policy,route_kind,model,connector_id,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET
                    policy=excluded.policy,route_kind=excluded.route_kind,
                    model=excluded.model,connector_id=excluded.connector_id,
                    last_error=NULL,updated_at=excluded.updated_at
                """,
                (
                    user_id,
                    payload.policy,
                    payload.route_kind,
                    model,
                    connector_id,
                    timestamp,
                    timestamp,
                ),
            )
        response = JSONResponse(_llm_settings_data(user_id))
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/api/account/llm-connectors")
    def create_llm_connector_api(
        payload: EdgeConnectorPayload,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        user_id = str(user["id"])
        dependencies.enforce_rate(
            request, "llm-connector-create", limit=5, seconds=3600, subject=user_id
        )
        connector_name = payload.name.strip()
        if not connector_name:
            raise HTTPException(400, "Enter a connector name.")
        connector_id = str(uuid.uuid4())
        token = f"rati_edge_{secrets.token_urlsafe(32)}"
        timestamp = dependencies.iso()
        with connection() as database:
            active_count = database.execute(
                """
                SELECT COUNT(*) FROM llm_edge_connectors
                WHERE user_id=? AND status='active'
                """,
                (user_id,),
            ).fetchone()[0]
            if int(active_count) >= 5:
                raise HTTPException(409, "Revoke an old connector before creating another one.")
            database.execute(
                """
                INSERT INTO llm_edge_connectors(
                    id,user_id,name,token_hash,status,created_at,updated_at
                ) VALUES(?,?,?,?,'active',?,?)
                """,
                (
                    connector_id,
                    user_id,
                    connector_name,
                    connector_token_hash(token),
                    timestamp,
                    timestamp,
                ),
            )
        response = JSONResponse(
            {
                "connector": {
                    "id": connector_id,
                    "name": connector_name,
                    "status": "active",
                    "last_seen_at": None,
                },
                "token": token,
                "token_notice": "This token is shown once. Keep it private.",
            },
            status_code=201,
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.delete("/api/account/llm-connectors/{connector_id}")
    def revoke_llm_connector_api(
        connector_id: str,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        user_id = str(user["id"])
        dependencies.enforce_rate(
            request, "llm-connector-revoke", limit=10, seconds=3600, subject=user_id
        )
        timestamp = dependencies.iso()
        with connection() as database:
            connector = database.execute(
                """
                SELECT id FROM llm_edge_connectors
                WHERE id=? AND user_id=? AND status='active'
                """,
                (connector_id, user_id),
            ).fetchone()
            if not connector:
                raise HTTPException(404, "Active local connector not found.")
            jobs = database.execute(
                """
                SELECT commission_id FROM llm_edge_jobs
                WHERE connector_id=? AND status IN ('pending','claimed')
                """,
                (connector_id,),
            ).fetchall()
            database.execute(
                """
                UPDATE llm_edge_jobs
                SET status='failed',error=?,completed_at=?,updated_at=?
                WHERE connector_id=? AND status IN ('pending','claimed')
                """,
                ("The local connector was revoked.", timestamp, timestamp, connector_id),
            )
            database.execute(
                """
                UPDATE user_llm_routes
                SET policy='managed',route_kind='managed',model='',connector_id=NULL,
                    last_error=NULL,updated_at=?
                WHERE user_id=? AND connector_id=?
                """,
                (timestamp, user_id, connector_id),
            )
            database.execute(
                """
                UPDATE llm_edge_connectors SET status='revoked',updated_at=?
                WHERE id=?
                """,
                (timestamp, connector_id),
            )
        for job in jobs:
            try:
                dependencies.run_research_commission(str(job["commission_id"]))
            except Exception:
                LOG.debug("Revoked edge job %s could not run", job["id"], exc_info=True)
        response = JSONResponse(_llm_settings_data(user_id))
        response.headers["Cache-Control"] = "no-store"
        return response

    def _edge_connector_for_request(request: Request) -> dict[str, Any]:
        dependencies.enforce_rate(request, "edge-auth", limit=180, seconds=60)
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "Missing connector token.")
        timestamp = dependencies.iso()
        with connection() as database:
            row = database.execute(
                """
                SELECT * FROM llm_edge_connectors
                WHERE token_hash=? AND status='active'
                """,
                (connector_token_hash(token),),
            ).fetchone()
            if not row:
                raise HTTPException(401, "Invalid connector token.")
            database.execute(
                "UPDATE llm_edge_connectors SET last_seen_at=?,updated_at=? WHERE id=?",
                (timestamp, timestamp, row["id"]),
            )
        return dict(row)

    @router.post("/api/llm/edge/jobs/claim")
    def claim_edge_job_api(request: Request) -> JSONResponse:
        connector = _edge_connector_for_request(request)
        connector_id = str(connector["id"])
        dependencies.enforce_rate(
            request, "edge-job-claim", limit=120, seconds=60, subject=connector_id
        )
        current_time = dependencies.now()
        timestamp = dependencies.iso(current_time)
        lease_expires_at = dependencies.iso(
            current_time + timedelta(minutes=dependencies.edge_job_lease_minutes)
        )
        with connection() as database:
            row = database.execute(
                """
                SELECT * FROM llm_edge_jobs
                WHERE connector_id=? AND (
                    status='pending' OR (status='claimed' AND lease_expires_at<=?)
                )
                ORDER BY created_at LIMIT 1
                """,
                (connector_id, timestamp),
            ).fetchone()
            if not row:
                return JSONResponse({"job": None})
            claimed = database.execute(
                """
                UPDATE llm_edge_jobs
                SET status='claimed',claimed_at=?,lease_expires_at=?,updated_at=?
                WHERE id=? AND connector_id=? AND (
                    status='pending' OR (status='claimed' AND lease_expires_at<=?)
                )
                """,
                (
                    timestamp,
                    lease_expires_at,
                    timestamp,
                    row["id"],
                    connector_id,
                    timestamp,
                ),
            )
            if claimed.rowcount != 1:
                return JSONResponse({"job": None})
        response = JSONResponse(
            {
                "job": {
                    "id": str(row["id"]),
                    "model": str(row["model"]),
                    "request": dependencies.json_container(row["request_json"], {}),
                    "request_fingerprint": str(row["request_fingerprint"]),
                    "lease_expires_at": lease_expires_at,
                }
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/api/llm/edge/jobs/{job_id}/heartbeat")
    def heartbeat_edge_job_api(job_id: str, request: Request) -> JSONResponse:
        connector = _edge_connector_for_request(request)
        connector_id = str(connector["id"])
        dependencies.enforce_rate(
            request, "edge-job-heartbeat", limit=120, seconds=60, subject=connector_id
        )
        current_time = dependencies.now()
        with connection() as database:
            updated = database.execute(
                """
                UPDATE llm_edge_jobs SET lease_expires_at=?,updated_at=?
                WHERE id=? AND connector_id=? AND status='claimed'
                """,
                (
                    dependencies.iso(
                        current_time + timedelta(minutes=dependencies.edge_job_lease_minutes)
                    ),
                    dependencies.iso(current_time),
                    job_id,
                    connector_id,
                ),
            )
        if updated.rowcount != 1:
            raise HTTPException(404, "Claimed local model job not found.")
        return JSONResponse({"ok": True})

    @router.post("/api/llm/edge/jobs/{job_id}/complete")
    async def complete_edge_job_api(
        job_id: str,
        payload: EdgeJobCompletePayload,
        request: Request,
    ) -> JSONResponse:
        connector = _edge_connector_for_request(request)
        connector_id = str(connector["id"])
        dependencies.enforce_rate(
            request, "edge-job-complete", limit=60, seconds=60, subject=connector_id
        )
        response_json = json.dumps(payload.response, separators=(",", ":"))
        if len(response_json.encode()) > 8 * 1024 * 1024:
            raise HTTPException(413, "Local model response is too large.")
        timestamp = dependencies.iso()
        with connection() as database:
            row = database.execute(
                """
                SELECT commission_id FROM llm_edge_jobs
                WHERE id=? AND connector_id=? AND status='claimed'
                """,
                (job_id, connector_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Claimed local model job not found.")
            database.execute(
                """
                UPDATE llm_edge_jobs
                SET status='complete',response_json=?,completed_at=?,updated_at=?
                WHERE id=?
                """,
                (response_json, timestamp, timestamp, job_id),
            )
        try:
            report = await run_in_threadpool(
                dependencies.run_research_commission, str(row["commission_id"])
            )
        except Exception as exc:
            LOG.warning("Local model report %s was rejected: %s", job_id, type(exc).__name__)
            raise HTTPException(
                422, "The local model response did not match the report contract."
            ) from exc
        return JSONResponse(
            {
                "ok": True,
                "report": dependencies.commission_api_payload(
                    report, str(connector["user_id"])
                ),
            }
        )

    @router.post("/api/llm/edge/jobs/{job_id}/fail")
    async def fail_edge_job_api(
        job_id: str,
        payload: EdgeJobFailPayload,
        request: Request,
    ) -> JSONResponse:
        connector = _edge_connector_for_request(request)
        connector_id = str(connector["id"])
        dependencies.enforce_rate(
            request, "edge-job-fail", limit=60, seconds=60, subject=connector_id
        )
        timestamp = dependencies.iso()
        with connection() as database:
            row = database.execute(
                """
                SELECT commission_id FROM llm_edge_jobs
                WHERE id=? AND connector_id=? AND status='claimed'
                """,
                (job_id, connector_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, "Claimed local model job not found.")
            database.execute(
                """
                UPDATE llm_edge_jobs
                SET status='failed',error=?,completed_at=?,updated_at=? WHERE id=?
                """,
                (payload.error[:500], timestamp, timestamp, job_id),
            )
        try:
            await run_in_threadpool(
                dependencies.run_research_commission, str(row["commission_id"])
            )
        except Exception:
            LOG.debug("Failed edge job %s could not run", job_id, exc_info=True)
        return JSONResponse({"ok": True})

    return LLMEdgeRoutes(
        router=router,
        model_settings_page=model_settings_page,
        account_llm_route_api=account_llm_route_api,
        update_account_llm_route_api=update_account_llm_route_api,
        create_llm_connector_api=create_llm_connector_api,
        revoke_llm_connector_api=revoke_llm_connector_api,
        claim_edge_job_api=claim_edge_job_api,
        heartbeat_edge_job_api=heartbeat_edge_job_api,
        complete_edge_job_api=complete_edge_job_api,
        fail_edge_job_api=fail_edge_job_api,
    )