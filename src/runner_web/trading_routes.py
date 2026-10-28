from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from runner_trading import ledger
from runner_trading.config import account_id
from runner_trading.dashboard import snapshot
from runner_web.db import connection


def operator(user: dict | None) -> bool:
    allowed = {i.strip() for i in os.getenv("TRADING_OPERATOR_IDS", "").split(",") if i.strip()}
    return bool(user and user["id"] in allowed)


class TradingAction(BaseModel):
    action: Literal["pause", "resume", "exit"]
    position_id: str | None = Field(default=None, max_length=64)


@dataclass(frozen=True)
class TradingDependencies:
    templates: Jinja2Templates
    page_context: Callable[..., dict[str, Any]]
    require_origin: Callable[..., None]
    require_user: Callable[..., dict]
    require_recent_auth: Callable[..., None]
    enforce_rate: Callable[..., None]


def create_trading_routes(deps: TradingDependencies) -> APIRouter:
    router = APIRouter()

    def require_operator(session):
        user = deps.require_user(session)
        if not operator(user):
            raise HTTPException(403, "Trading operator access required")
        return user

    @router.get("/trading")
    def trading_page(
        request: Request,
        mode: Literal["paper", "live"] = "paper",
        runner_session: str | None = Cookie(default=None),
    ):
        deps.enforce_rate(request, "trading-read", limit=60, seconds=60)
        if mode == "live":
            require_operator(runner_session)
        context = deps.page_context(request, runner_session, nav_product="memecoins")
        context.update(
            trading=snapshot(mode),
            can_control=operator(context.get("user")),
            screen={"market": "trading", "kind": "trading", "label": "Trading"},
        )
        return deps.templates.TemplateResponse(
            request, "trading.html", context, headers={"Cache-Control": "private, no-store"}
        )

    @router.get("/api/trading")
    def trading_api(
        request: Request,
        mode: Literal["paper", "live"] = "paper",
        runner_session: str | None = Cookie(default=None),
    ):
        deps.enforce_rate(request, "trading-read", limit=60, seconds=60)
        if mode == "live":
            require_operator(runner_session)
        return JSONResponse(snapshot(mode), headers={"Cache-Control": "private, no-store"})

    @router.post("/api/trading/control")
    def trading_control(
        body: TradingAction,
        request: Request,
        mode: Literal["paper", "live"] = "paper",
        runner_session: str | None = Cookie(default=None),
    ):
        deps.require_origin(request)
        user = require_operator(runner_session)
        deps.require_recent_auth(runner_session)
        deps.enforce_rate(request, "trading-control", limit=20, seconds=60)
        identifier, at = account_id(mode), datetime.now(UTC)
        try:
            ledger.account(identifier)
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc
        if body.action in {"pause", "resume"}:
            ledger.pause(
                identifier,
                "Operator paused entries" if body.action == "pause" else "",
                at,
                paused=body.action == "pause",
            )
        else:
            with connection() as db:
                changed = db.execute(
                    "UPDATE trading_positions SET exit_requested=1 WHERE id=? "
                    "AND account_id=? AND status='open'",
                    (body.position_id, identifier),
                )
                if changed.rowcount != 1:
                    raise HTTPException(404, "Open position required")
                ledger.event(
                    db,
                    identifier,
                    "exit_request",
                    {"position_id": body.position_id, "operator_id": user["id"]},
                    at,
                )
        return JSONResponse(
            {"ok": True, "action": body.action}, headers={"Cache-Control": "private, no-store"}
        )

    @router.get("/health/trading")
    def trading_health():
        data = snapshot("paper")
        return JSONResponse(
            {
                "state": data["state"],
                "mode": "paper",
                "heartbeat_at": data.get("heartbeat_at"),
                "open_positions": data.get("open_positions", 0),
            },
            status_code=200 if data["state"] == "ok" else 503,
            headers={"Cache-Control": "no-store"},
        )

    return router
