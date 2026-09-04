from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

LOG = logging.getLogger(__name__)


class CloudDataDeletePayload(BaseModel):
    confirmation: Literal["MOVE MY DATA"]


class AccountDeletePayload(BaseModel):
    confirmation: Literal["DELETE MY ACCOUNT"]


@dataclass(frozen=True)
class AccountRouteDependencies:
    templates: Jinja2Templates
    page_context: Callable[..., dict[str, Any]]
    require_origin: Callable[[Request], None]
    require_user: Callable[[str | None], dict[str, Any]]
    enforce_rate: Callable[..., None]
    require_recent_auth: Callable[[str | None], None]
    user_data_summary: Callable[[str], dict[str, Any]]
    export_user_data: Callable[[str], dict[str, Any]]
    delete_user_content: Callable[[str], dict[str, Any]]
    delete_customer: Callable[[dict[str, Any]], bool]
    delete_user_data: Callable[[str], dict[str, Any]]
    now: Callable[[], datetime]
    session_cookie: str
    cookie_domain: str | None


@dataclass(frozen=True)
class AccountRoutes:
    router: APIRouter
    privacy_page: Callable[..., HTMLResponse]
    account_export_api: Callable[..., JSONResponse]
    account_cloud_data_delete_api: Callable[..., JSONResponse]
    account_delete_api: Callable[..., JSONResponse]


def create_account_routes(dependencies: AccountRouteDependencies) -> AccountRoutes:
    router = APIRouter()

    @router.get("/privacy", response_class=HTMLResponse)
    def privacy_page(
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> HTMLResponse:
        context = dependencies.page_context(request, runner_session)
        user = context.get("user")
        if user:
            context["data_summary"] = dependencies.user_data_summary(str(user["id"]))
        return dependencies.templates.TemplateResponse(
            request=request,
            name="privacy.html",
            context=context,
        )

    @router.get("/api/account/export")
    def account_export_api(
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        user = dependencies.require_user(runner_session)
        dependencies.enforce_rate(
            request,
            "account-export",
            limit=3,
            seconds=3600,
            subject=user["id"],
        )
        response = JSONResponse(dependencies.export_user_data(str(user["id"])))
        response.headers["Content-Disposition"] = (
            "attachment; filename="
            f'"runner-watch-export-{dependencies.now().date().isoformat()}.json"'
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/api/account/data/delete-cloud-copy")
    def account_cloud_data_delete_api(
        payload: CloudDataDeletePayload,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        dependencies.enforce_rate(
            request,
            "account-cloud-data-delete",
            limit=3,
            seconds=3600,
            subject=user["id"],
        )
        result = dependencies.delete_user_content(str(user["id"]))
        if not result["deleted"]:
            raise HTTPException(404, "Account not found")
        response = JSONResponse(result)
        response.headers["Cache-Control"] = "no-store"
        return response

    @router.post("/api/account/delete")
    def account_delete_api(
        payload: AccountDeletePayload,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        dependencies.require_recent_auth(runner_session)
        dependencies.enforce_rate(
            request,
            "account-delete",
            limit=3,
            seconds=3600,
            subject=user["id"],
        )
        try:
            dependencies.delete_customer(user)
        except Exception as exc:
            LOG.warning(
                "Could not delete Stripe customer during account deletion: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                502,
                "We could not stop billing, so the local account was not deleted. Try again.",
            ) from exc
        result = dependencies.delete_user_data(str(user["id"]))
        if not result["deleted"]:
            raise HTTPException(404, "Account not found")
        response = JSONResponse({"deleted": True})
        response.headers["Cache-Control"] = "no-store"
        response.delete_cookie("runner_visitor", path="/")
        response.delete_cookie(
            dependencies.session_cookie,
            path="/",
            domain=dependencies.cookie_domain,
        )
        return response

    return AccountRoutes(
        router=router,
        privacy_page=privacy_page,
        account_export_api=account_export_api,
        account_cloud_data_delete_api=account_cloud_data_delete_api,
        account_delete_api=account_delete_api,
    )
