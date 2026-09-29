from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from runner_web.operations import require_operations_access

TELEGRAM_WEBHOOK_MAX_BYTES = 1_048_576


@dataclass(frozen=True)
class TelegramRouteDependencies:
    templates: Jinja2Templates
    page_context: Callable[..., dict[str, Any]]
    webhook_secret: Callable[[], str]
    store_update: Callable[[dict[str, Any]], None]
    run_in_threadpool: Callable[..., Awaitable[Any]]
    runners_origin: Callable[[], str]


@dataclass(frozen=True)
class TelegramRoutes:
    router: APIRouter
    telegram_webhook: Callable[..., Any]
    telegram_announcements_page: Callable[..., Any]
    telegram_announcements_api: Callable[..., Any]
    publish_signal: Callable[..., None]
    signal_page: Callable[..., RedirectResponse]
    signal_card: Callable[..., None]
    report_signal: Callable[..., None]


def create_telegram_routes(dependencies: TelegramRouteDependencies) -> TelegramRoutes:
    router = APIRouter()

    @router.post("/telegram/webhook")
    async def telegram_webhook(request: Request) -> JSONResponse:
        """Take one update from Telegram and store it for the chat worker.

        This answers quickly and does no thinking, because Telegram retries anything
        it is not answered promptly and a slow handler turns into duplicate replies.
        The secret header is the only thing standing between this public path and
        anyone posting forged updates, so an unset secret closes the door entirely.
        """

        webhook_secret = dependencies.webhook_secret()
        if not webhook_secret:
            raise HTTPException(404, "Not found")
        supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not secrets.compare_digest(supplied, webhook_secret):
            raise HTTPException(404, "Not found")
        raw = await request.body()
        if len(raw) > TELEGRAM_WEBHOOK_MAX_BYTES:
            raise HTTPException(413, "Update too large")
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Malformed update") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "Malformed update")
        await dependencies.run_in_threadpool(dependencies.store_update, payload)
        return JSONResponse({"ok": True})

    @router.get("/telegram/announcements", response_class=HTMLResponse)
    def telegram_announcements_page(request: Request):
        return dependencies.templates.TemplateResponse(
            request,
            "telegram_announcements.html",
            dependencies.page_context(request, None, resolved_user=None),
        )

    @router.get("/api/telegram/announcements")
    def telegram_announcements_api(_access: None = Depends(require_operations_access)):
        from runner_web.telegram_outbox import announcement_history

        return JSONResponse(announcement_history(), headers={"Cache-Control": "no-store"})

    @router.post("/api/signals")
    def publish_signal() -> None:

        raise HTTPException(410, "Public Signals were replaced by Calls.")

    @router.get("/s/{public_id}", response_class=HTMLResponse)
    def signal_page(
        public_id: str,
    ) -> RedirectResponse:
        _ = public_id
        return RedirectResponse(f"{dependencies.runners_origin()}/community", status_code=308)

    @router.get("/s/{public_id}/card.png")
    def signal_card(public_id: str) -> None:
        _ = public_id
        raise HTTPException(410, "Public Signals were replaced by Calls.")

    @router.post("/api/signals/{public_id}/report")
    def report_signal(public_id: str) -> None:
        _ = public_id
        raise HTTPException(410, "Public Signals were replaced by Calls.")

    return TelegramRoutes(
        router=router,
        telegram_webhook=telegram_webhook,
        telegram_announcements_page=telegram_announcements_page,
        telegram_announcements_api=telegram_announcements_api,
        publish_signal=publish_signal,
        signal_page=signal_page,
        signal_card=signal_card,
        report_signal=report_signal,
    )
