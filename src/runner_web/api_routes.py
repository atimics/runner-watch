"""Versioned, metered API and account key management."""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from runner_web import api_credits, api_data, api_limits, api_payments
from runner_web.db import connection

bearer = HTTPBearer(auto_error=False)


def user_id(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str:
    value = f"{credentials.scheme} {credentials.credentials}" if credentials else ""
    return api_credits.authenticate(value)


Actor = Annotated[str, Depends(user_id)]


class APIBodyLimit:
    """Bound API JSON before FastAPI parses it, including chunked requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        applies = (
            scope["type"] == "http"
            and scope.get("method") in {"POST", "PUT", "PATCH"}
            and scope.get("path", "").startswith(("/api/v1/", "/api/developer/"))
        )
        if not applies:
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > 16384:
                response = JSONResponse(
                    {"detail": "Use an API request body up to 16 KiB."}, status_code=413
                )
                return await response(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        pending = True

        async def limited_receive():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        return await self.app(scope, limited_receive, send)


class KeyInput(BaseModel):
    name: str = Field(default="API key", min_length=1, max_length=60)


class PackInput(BaseModel):
    dollars: Literal[1, 5, 20]


class JobInput(BaseModel):
    address: str = Field(min_length=32, max_length=44)
    cursor: str | None = Field(default=None, min_length=64, max_length=64)


def request_key(request: Request, *, required: bool = False) -> str:
    key = request.headers.get("Idempotency-Key")
    if key is None and not required:
        return secrets.token_hex(16)
    if key is None or re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", key) is None:
        raise HTTPException(
            422, "Send an Idempotency-Key with 8 to 128 letters, digits, dashes, or underscores."
        )
    return key


def pricing() -> dict[str, Any]:
    try:
        config = api_payments.configuration()
    except HTTPException:
        config = {"mode": "setup", "enabled": False}
    return {
        "version": "v1",
        "credits_per_usdc": 1000,
        "packs": api_credits.PACKS,
        "free_monthly_credits": 1000,
        "free_daily_limit": 100,
        "rates": {
            "free": {"requests_per_minute": 10, "concurrent": 1},
            "paid": {"requests_per_minute": 60, "concurrent": 5},
            "fresh_jobs": {"starts_per_minute": 2, "active_per_account": 1},
        },
        "prices": api_credits.PRICES,
        "payments": {"enabled": config["enabled"], "environment": config["mode"]},
        "fresh_jobs_enabled": os.getenv("API_FRESH_JOBS_ENABLED", "0") == "1",
        "result_replay_seconds": 3600,
        "job_retention_days": 7,
    }


def envelope(row: dict[str, Any], data: Any, *, replayed: bool = False) -> dict[str, Any]:
    return {
        "data": data,
        "usage": {
            "request_id": row["id"],
            "credits": 0 if replayed else row["free_cost"] + row["paid_cost"],
            "original_credits": row["free_cost"] + row["paid_cost"],
            "replayed": replayed,
        },
    }


def metered(
    request: Request,
    owner: str,
    operation: str,
    parameters: dict[str, Any],
    read: Callable[[], Any],
) -> JSONResponse:
    current = api_credits.balance(owner)
    with api_limits.admission(owner, paid=current["tier"] == "paid"):
        row, fresh = api_credits.reserve(owner, request_key(request), operation, parameters)
        if not fresh:
            if row["status"] == "failed":
                raise HTTPException(
                    409, "This request was refunded. Use a new Idempotency-Key to retry."
                )
            if row["status"] != "complete":
                raise HTTPException(
                    409, "This request is in progress.", headers={"Retry-After": "5"}
                )
            response = envelope(row, api_limits.load_result(row["id"]), replayed=True)
        else:
            try:
                data = read()
                api_limits.save_result(row["id"], data)
                api_credits.finish(row["id"])
                response = envelope(row, data)
            except Exception:
                api_credits.finish(row["id"], error="This request failed. Credits returned.")
                raise
        response["balance"] = api_credits.balance(owner)
        return JSONResponse(
            response,
            headers={
                "Cache-Control": "private, no-store",
                "X-Credits-Charged": str(response["usage"]["credits"]),
                "X-Request-ID": row["id"],
            },
        )


def topup_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "credits": row["credits"],
        "dollars": row["dollars"],
        "payment_url": row["resource"],
        "settlement": json.loads(row["settlement_json"]) if row["settlement_json"] else None,
    }


def create_api_routes(
    *,
    require_user: Callable[..., Any],
    require_origin: Callable[..., None],
    enforce_rate: Callable[..., None],
    page: Callable[..., Any],
    origin: Callable[[], str],
) -> APIRouter:
    def edge_limit(request: Request):
        if request.url.path.startswith("/api/v1/"):
            enforce_rate(request, "developer-api-ip", limit=120, seconds=60)

    router = APIRouter(tags=["Runner Watch API v1"], dependencies=[Depends(edge_limit)])

    @router.get("/developers")
    def developers(request: Request, runner_session: str | None = Cookie(default=None)):
        return page(request, runner_session, pricing())

    @router.get("/api/v1/openapi.json", include_in_schema=False)
    def openapi():
        schema = get_openapi(
            title="Runner Watch API",
            version="1.0.0",
            routes=[route for route in router.routes if route.path.startswith("/api/v1/")],
        )
        schema["servers"] = [{"url": origin()}]
        return schema

    @router.get("/api/v1/pricing")
    def prices() -> dict[str, Any]:
        return pricing()

    @router.post("/api/developer/keys", status_code=201)
    def create_key(
        body: KeyInput, request: Request, runner_session: str | None = Cookie(default=None)
    ):
        require_origin(request)
        user = require_user(runner_session)
        enforce_rate(request, "api-key-create", limit=5, seconds=3600, subject=user["id"])
        enforce_rate(request, "api-key-create-ip", limit=10, seconds=3600)
        return JSONResponse(
            api_credits.create_key(user["id"], body.name.strip()),
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/developer/account")
    def developer_account(request: Request, runner_session: str | None = Cookie(default=None)):
        user = require_user(runner_session)
        enforce_rate(request, "api-account", limit=30, seconds=60, subject=user["id"])
        with connection() as db:
            keys = [
                dict(row)
                for row in db.execute(
                    "SELECT id,name,created_at FROM api_keys "
                    "WHERE user_id=? AND revoked_at IS NULL ORDER BY created_at",
                    (user["id"],),
                ).fetchall()
            ]
        return JSONResponse(
            {"keys": keys, "balance": api_credits.balance(user["id"])},
            headers={"Cache-Control": "no-store"},
        )

    @router.delete("/api/developer/keys/{identifier}")
    def revoke_key(
        identifier: str, request: Request, runner_session: str | None = Cookie(default=None)
    ):
        require_origin(request)
        user = require_user(runner_session)
        with connection() as db:
            db.execute(
                "UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=?",
                (api_credits.stamp(), identifier, user["id"]),
            )
        return {"revoked": True}

    @router.get("/api/v1/balance")
    def credits(owner: Actor):
        current = api_credits.balance(owner)
        with api_limits.admission(owner, paid=current["tier"] == "paid"):
            return JSONResponse(current, headers={"Cache-Control": "no-store"})

    @router.get("/api/v1/tokens")
    def tokens(
        request: Request,
        owner: Actor,
        cursor: str = Query(default="", max_length=128),
        limit: int = Query(default=50, ge=1, le=50),
    ):
        return metered(
            request,
            owner,
            "tokens",
            {"cursor": cursor, "limit": limit},
            lambda: api_data.tokens(cursor, limit),
        )

    @router.get("/api/v1/tokens/{coin_id}")
    def snapshot(coin_id: str, request: Request, owner: Actor):
        return metered(
            request, owner, "snapshot", {"coin_id": coin_id}, lambda: api_data.snapshot(coin_id)
        )

    @router.get("/api/v1/tokens/{coin_id}/analysis")
    def analysis(coin_id: str, request: Request, owner: Actor):
        return metered(
            request, owner, "analysis", {"coin_id": coin_id}, lambda: api_data.analysis(coin_id)
        )

    @router.get("/api/v1/tokens/{coin_id}/history")
    def history(
        coin_id: str,
        request: Request,
        owner: Actor,
        cursor: str = Query(default="", max_length=64),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        return metered(
            request,
            owner,
            "history",
            {"coin_id": coin_id, "cursor": cursor, "limit": limit},
            lambda: api_data.history(coin_id, cursor, limit),
        )

    @router.get("/api/v1/wallets/{address}")
    def wallet(address: str, request: Request, owner: Actor):
        api_data.address(address)
        return metered(
            request, owner, "wallet", {"address": address}, lambda: api_data.wallet(address)
        )

    @router.post("/api/v1/jobs/{operation}", status_code=202)
    def start_job(
        operation: Literal["token_check", "wallet_scan"],
        body: JobInput,
        request: Request,
        owner: Actor,
    ):
        if os.getenv("API_FRESH_JOBS_ENABLED", "0") != "1":
            raise HTTPException(503, "Fresh scans are preparing. Saved data is available.")
        if not os.getenv("HELIUS_API_KEY", "").strip():
            raise HTTPException(503, "The chain provider is preparing.")
        api_data.address(body.address)
        if operation == "token_check" and body.cursor:
            raise HTTPException(422, "A token check uses an address.")
        if body.cursor:
            previous = api_credits.receipt(body.cursor, owner)
            if (
                previous["operation"] != "wallet_scan"
                or previous["status"] != "complete"
                or json.loads(previous["parameters"])["address"] != body.address
            ):
                raise HTTPException(422, "Use a completed scan cursor for this wallet.")
        current = api_credits.balance(owner)
        with api_limits.admission(owner, paid=current["tier"] == "paid", jobs=True):
            row, fresh = api_credits.reserve(
                owner,
                request_key(request, required=True),
                operation,
                body.model_dump(),
                paid_only=True,
            )
        return {
            "job_id": row["id"],
            "status": row["status"],
            "replayed": not fresh,
            "reserved_credits": row["paid_cost"] if row["status"] in {"queued", "running"} else 0,
            "status_url": "/api/v1/jobs/" + row["id"],
        }

    @router.get("/api/v1/jobs/{identifier}")
    def job(identifier: str, owner: Actor):
        current = api_credits.balance(owner)
        with api_limits.admission(owner, paid=current["tier"] == "paid"):
            row = api_credits.receipt(identifier, owner)
        result = json.loads(row["result_json"]) if row["result_json"] else None
        if result:
            result.pop("_state", None)
        return JSONResponse(
            {
                "id": identifier,
                "status": row["status"],
                "result": result,
                "error": row["error"],
                "credits": row["paid_cost"] if row["status"] == "complete" else 0,
                "reserved_credits": row["paid_cost"]
                if row["status"] in {"queued", "running"}
                else 0,
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/v1/topups", status_code=201)
    def new_order(body: PackInput, request: Request, owner: Actor):
        current = api_credits.balance(owner)
        with api_limits.admission(owner, paid=current["tier"] == "paid"):
            row = api_payments.order(
                owner, body.dollars, request_key(request, required=True), origin()
            )
        return JSONResponse(topup_view(row), status_code=201, headers={"Cache-Control": "no-store"})

    @router.post("/api/v1/topups/{identifier}")
    def pay_order(identifier: str, request: Request, owner: Actor):
        current = api_credits.balance(owner)
        with api_limits.admission(owner, paid=current["tier"] == "paid"):
            row = api_payments.get_order(identifier, owner)
            if row["status"] != "complete":
                if not api_payments.configuration()["enabled"]:
                    raise HTTPException(503, "Payment setup is pending.")
                signature = request.headers.get("PAYMENT-SIGNATURE")
                if not signature:
                    challenge = api_payments.challenge(row)
                    return JSONResponse(
                        challenge,
                        status_code=402,
                        headers={
                            "PAYMENT-REQUIRED": api_payments.encode(challenge),
                            "Cache-Control": "no-store",
                        },
                    )
                row = api_payments.settle(identifier, owner, signature)
        headers = {
            "Cache-Control": "no-store",
            "PAYMENT-RESPONSE": api_payments.encode(json.loads(row["settlement_json"])),
        }
        return JSONResponse(
            {**topup_view(row), "balance": api_credits.balance(owner)}, headers=headers
        )

    @router.get("/api/v1/topups/{identifier}")
    def order_status(identifier: str, owner: Actor):
        current = api_credits.balance(owner)
        with api_limits.admission(owner, paid=current["tier"] == "paid"):
            return JSONResponse(
                topup_view(api_payments.get_order(identifier, owner)),
                headers={"Cache-Control": "no-store"},
            )

    return router
