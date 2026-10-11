from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from runner_web.memecoin_calls import (
    close_memecoin_call,
    create_memecoin_call,
    memecoin_calls,
    pending_memecoin_order,
)
from runner_web.memecoin_lookup import lookup_memecoin
from runner_web.memecoins import request_memecoin
from runner_web.share_cards import memecoin_share

MEMECOIN_CHART_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass(frozen=True)
class MemecoinRouteDependencies:
    enforce_rate: Callable[..., None]
    now: Callable[[], datetime]
    page_context: Callable[..., dict[str, Any]]
    require_origin: Callable[[Request], None]
    require_user: Callable[[str | None], dict[str, Any]]
    templates: Jinja2Templates
    board_view: Callable[[str | None], str]
    simple_board: Callable[..., HTMLResponse]
    conditional_json_response: Callable[[Request, Any], Response]
    expected_call_price: Callable[[Request], Awaitable[float | None]]
    flash_report_action: Callable[..., dict[str, Any]]
    invalidate_public_screen_data: Callable[[str, str], None]
    public_screen_data: Callable[..., Any]
    latest_commission: Callable[[str, str], dict[str, Any] | None]
    daily_report_for_ticker: Callable[..., Any]
    cached_memecoin_detail: Callable[[str], dict[str, Any] | None]
    memecoin_detail_payload: Callable[[str], dict[str, Any]]
    memecoin_market: Callable[..., dict[str, Any]]
    active_memecoin_call: Callable[..., Any]
    wallet_for_user: Callable[[str], dict[str, Any]]
    run_in_threadpool: Callable[..., Awaitable[Any]]
    board_view_tabs: dict[str, str]
    default_board_view: str


@dataclass(frozen=True)
class MemecoinRoutes:
    router: APIRouter
    memecoins_page: Callable[..., Any]
    memecoins_board_response: Callable[..., Any]
    memecoins_radar_page: Callable[..., Any]
    memecoin_transaction_evidence: Callable[..., Any]
    memecoin_charts_api: Callable[..., Any]
    memecoins_api: Callable[..., Any]
    memecoin_alpha_redirect: Callable[..., Any]
    memecoin_calls_api: Callable[..., Any]
    memecoin_detail_api: Callable[..., Any]
    memecoin_replay_api: Callable[..., Any]
    memecoin_replay_gif_api: Callable[..., Any]
    memecoin_replay_evidence_api: Callable[..., Any]
    memecoin_replay_receipt_api: Callable[..., Any]
    memecoin_detail_page: Callable[..., Any]
    make_memecoin_call_api: Callable[..., Any]
    close_memecoin_call_api: Callable[..., Any]


def create_memecoin_routes(dependencies: MemecoinRouteDependencies) -> MemecoinRoutes:
    router = APIRouter()

    @router.get("/memecoins", response_class=HTMLResponse)
    def memecoins_page(
        request: Request,
        runner_session: str | None = Cookie(default=None),
        q: str = "",
        sort: str = "volume",
        view: str = dependencies.default_board_view,
    ) -> Response:
        return memecoins_board_response(
            request, runner_session, dependencies.board_view(view), q, sort
        )

    def memecoins_board_response(
        request: Request,
        runner_session: str | None,
        view: str,
        q: str = "",
        sort: str = "volume",
    ) -> HTMLResponse:
        from runner_web.stories import stories_by_subject

        dependencies.enforce_rate(request, "memecoins", limit=120, seconds=60)
        market = dependencies.memecoin_market(query=q, sort=sort, view="radar")
        coins = [str(item.get("id") or "") for item in market["rows"] if item.get("id")]
        # Hydrate a small snapshot now; the worker gathers history and evidence.
        requested = not market["rows"] and request_memecoin(q)
        token_lookup = lookup_memecoin(q, fetch=bool(requested)) if not market["rows"] else None
        return dependencies.simple_board(
            request,
            runner_session,
            "memecoins",
            market["rows"],
            view,
            q,
            updated_at=str(market.get("collected_at") or ""),
            stories=stories_by_subject("memecoins", coins),
            requested=q.strip() if requested else "",
            token_lookup=token_lookup,
        )

    @router.get("/memecoins/radar", response_class=HTMLResponse)
    def memecoins_radar_page(
        request: Request,
        runner_session: str | None = Cookie(default=None),
        league: str = "all",
    ) -> RedirectResponse:
        _ = request, runner_session, league
        return RedirectResponse("/memecoins?view=changed", status_code=307)

    @router.get("/api/memecoins/evidence/{signature}")
    def memecoin_transaction_evidence(request: Request, signature: str):
        from runner_web.memecoin_evidence import transaction_receipt

        dependencies.enforce_rate(request, "memecoin_evidence", limit=30, seconds=60)
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,88}", signature):
            raise HTTPException(404, "Transaction receipt unavailable")
        receipt = transaction_receipt(signature)
        if receipt is None:
            raise HTTPException(404, "Transaction receipt unavailable")
        return receipt

    @router.get("/api/memecoins/charts")
    async def memecoin_charts_api(request: Request, ids: str = "", offset: int = 0) -> Response:
        """Board sparklines, one bounded batch like the stock board's."""
        from runner_web.memecoin_store import memecoin_sparklines

        _ = offset  # The row ids already name the page.
        dependencies.enforce_rate(request, "memecoin-charts", limit=20, seconds=60)
        requested = sorted(
            {coin for coin in ids.split(",") if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", coin)}
        )[:50]
        key = ",".join(requested)
        cached = MEMECOIN_CHART_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 60:
            payload = cached[1]
        else:
            charts = await dependencies.run_in_threadpool(
                memecoin_sparklines, requested, at=dependencies.now()
            )
            payload = {"charts": charts, "annotations": {}}
            if len(MEMECOIN_CHART_CACHE) > 200:
                MEMECOIN_CHART_CACHE.clear()
            MEMECOIN_CHART_CACHE[key] = (time.monotonic(), payload)
        return dependencies.conditional_json_response(request, payload)

    @router.get("/api/memecoins")
    def memecoins_api(request: Request, q: str = "", sort: str = "volume", view: str = "radar"):
        dependencies.enforce_rate(request, "memecoins", limit=120, seconds=60)
        market = dependencies.memecoin_market(query=q, sort=sort, view=view)
        requested = bool(not market["rows"] and request_memecoin(q))
        token_lookup = lookup_memecoin(q, fetch=requested) if not market["rows"] else None
        return {**market, "requested": requested, "token_lookup": token_lookup}

    @router.get("/memecoins/alpha", response_class=HTMLResponse)
    def memecoin_alpha_redirect(
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> RedirectResponse:
        _ = request, runner_session
        return RedirectResponse("/memecoins?view=calls", status_code=307)

    @router.get("/api/memecoin-calls")
    def memecoin_calls_api(request: Request) -> dict[str, Any]:
        dependencies.enforce_rate(request, "memecoins", limit=120, seconds=60)
        return {"calls": memecoin_calls()}

    @router.get("/api/memecoins/{coin_id}")
    def memecoin_detail_api(coin_id: str, request: Request) -> dict[str, Any]:
        dependencies.enforce_rate(request, "memecoins", limit=120, seconds=60)
        return dependencies.memecoin_detail_payload(coin_id)

    @router.get("/api/memecoins/{coin_id}/replay")
    def memecoin_replay_api(coin_id: str, request: Request, revision: str | None = None):
        dependencies.enforce_rate(request, "memecoin_replay", limit=30, seconds=60)
        if dependencies.cached_memecoin_detail(coin_id) is None or (
            revision and not re.fullmatch(r"[a-f0-9]{64}", revision)
        ):
            raise HTTPException(404, "Replay not found")
        try:
            status = _cached_replay_status(coin_id, revision)
        except ValueError:
            raise HTTPException(409, "Saved replay needs an evidence review") from None
        if revision and status["status"] != "ready":
            raise HTTPException(404, "Replay not found")
        return status

    def _cached_replay_status(coin_id: str, revision: str | None) -> dict[str, Any]:
        from runner_web.memecoin_replay_store import replay_status

        return dependencies.public_screen_data(
            "memecoin-replay",
            f"{coin_id}:{revision or ''}",
            lambda: replay_status(coin_id, revision),
        )

    def _memecoin_replay_artifact(coin_id: str, replay_id: str, request: Request, *, gif: bool):
        from runner_web.memecoin_replay_store import saved_replay

        dependencies.enforce_rate(request, "memecoin_replay", limit=30, seconds=60)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", coin_id) or not re.fullmatch(
            r"[a-f0-9]{64}", replay_id
        ):
            raise HTTPException(404, "Replay not found")
        try:
            record = saved_replay(coin_id, replay_id, with_gif=gif)
        except ValueError:
            raise HTTPException(409, "Saved replay needs an evidence review") from None
        if record is None:
            raise HTTPException(404, "Replay not found")
        content = record["gif"] if gif else json.dumps(record["payload"], allow_nan=False).encode()
        suffix = "gif" if gif else "json"
        return Response(
            content,
            media_type="image/gif" if gif else "application/json",
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": '"' + (record["gif_sha256"] if gif else replay_id) + '"',
                "Content-Disposition": f'inline; filename="token-replay-{replay_id[:12]}.{suffix}"',
            },
        )

    @router.get("/api/memecoins/{coin_id}/replays/{replay_id}.gif")
    def memecoin_replay_gif_api(coin_id: str, replay_id: str, request: Request):
        return _memecoin_replay_artifact(coin_id, replay_id, request, gif=True)

    @router.get("/api/memecoins/{coin_id}/replays/{replay_id}.json")
    def memecoin_replay_evidence_api(coin_id: str, replay_id: str, request: Request):
        return _memecoin_replay_artifact(coin_id, replay_id, request, gif=False)

    @router.get("/api/memecoins/{coin_id}/replays/{replay_id}/receipts/{signature}")
    def memecoin_replay_receipt_api(coin_id: str, replay_id: str, signature: str, request: Request):
        package = _memecoin_replay_artifact(coin_id, replay_id, request, gif=False)
        payload = json.loads(package.body)
        receipt = next((row for row in payload["receipts"] if row["signature"] == signature), None)
        if receipt is None:
            raise HTTPException(404, "Receipt not found")
        return JSONResponse(
            receipt, headers={"Cache-Control": "public, max-age=31536000, immutable"}
        )

    @router.get("/memecoins/coin/{coin_id}", response_class=HTMLResponse)
    def memecoin_detail_page(
        coin_id: str,
        request: Request,
        runner_session: str | None = Cookie(default=None),
        view: str = "pulse",
        q: str = "",
        sort: str = "volume",
    ) -> HTMLResponse:
        dependencies.enforce_rate(request, "memecoins", limit=120, seconds=60)
        detail = dependencies.memecoin_detail_payload(coin_id)
        view = "radar" if view == "radar" else "pulse"
        list_path = "/memecoins"
        sort = sort if sort in {"volume", "market_cap", "gainers", "losers"} else "volume"
        back_url = (
            list_path
            + "?"
            + urlencode(
                {
                    "q": q.strip()[:80],
                    "sort": sort,
                    "view": "changed" if view == "radar" else "pulse",
                }
            )
        )
        context = dependencies.page_context(
            request,
            runner_session,
            nav_product="memecoins",
            active_tab=view,
            detail=detail,
            share=memecoin_share(detail, coin_id),
            calls=detail["calls"],
            back_url=back_url,
            list_path=list_path,
            list_view=view,
            query=q.strip()[:80],
            sort=sort,
        )
        if context["user"]:
            detail["pending_order"] = pending_memecoin_order(str(context["user"]["id"]), coin_id)
        context["active_call"] = (
            (
                dependencies.active_memecoin_call(str(context["user"]["id"]), coin_id)
                or next(
                    iter(
                        memecoin_calls(user_id=str(context["user"]["id"]), coin_id=coin_id, limit=1)
                    ),
                    None,
                )
            )
            if context["user"]
            else None
        )
        user_id = str(context["user"]["id"]) if context["user"] else None
        context["flash_report"] = dependencies.flash_report_action(
            user_id=user_id,
            latest_report=dependencies.daily_report_for_ticker(coin_id, user_id),
            latest_attempt=dependencies.latest_commission(user_id, coin_id) if user_id else None,
            start_url=f"/api/research/coin/{coin_id}",
            login_url=f"/login?next=/memecoins/coin/{coin_id}",
        )
        return dependencies.templates.TemplateResponse(request, "simple_coin_detail.html", context)

    @router.post("/api/memecoins/{coin_id}/calls")
    async def make_memecoin_call_api(
        coin_id: str,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        dependencies.enforce_rate(
            request, "call-create", limit=12, seconds=3600, subject=user["id"]
        )
        expected = await dependencies.expected_call_price(request)
        try:
            order = await dependencies.run_in_threadpool(
                create_memecoin_call, str(user["id"]), coin_id, expected_price=expected
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        dependencies.invalidate_public_screen_data("caller", str(order["caller_handle"]))
        # Accepted, not filled: the Call opens at the next quote.
        return JSONResponse(
            {"order": order, "balance": dependencies.wallet_for_user(str(user["id"]))["balance"]},
            status_code=202,
        )

    @router.post("/api/memecoin-calls/{public_id}/close")
    async def close_memecoin_call_api(
        public_id: str,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        dependencies.enforce_rate(request, "call-close", limit=12, seconds=3600, subject=user["id"])
        expected = await dependencies.expected_call_price(request)
        try:
            call = await dependencies.run_in_threadpool(
                close_memecoin_call, str(user["id"]), public_id, expected_price=expected
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if call is None:
            raise HTTPException(404, "Call not found")
        dependencies.invalidate_public_screen_data("caller", str(call["caller_handle"]))
        wallet = dependencies.wallet_for_user(str(user["id"]))
        return JSONResponse(
            {
                "call": call,
                "reward": int(call.get("flash_reward") or 0),
                "balance": wallet["balance"],
            }
        )

    return MemecoinRoutes(
        router=router,
        memecoins_page=memecoins_page,
        memecoins_board_response=memecoins_board_response,
        memecoins_radar_page=memecoins_radar_page,
        memecoin_transaction_evidence=memecoin_transaction_evidence,
        memecoin_charts_api=memecoin_charts_api,
        memecoins_api=memecoins_api,
        memecoin_alpha_redirect=memecoin_alpha_redirect,
        memecoin_calls_api=memecoin_calls_api,
        memecoin_detail_api=memecoin_detail_api,
        memecoin_replay_api=memecoin_replay_api,
        memecoin_replay_gif_api=memecoin_replay_gif_api,
        memecoin_replay_evidence_api=memecoin_replay_evidence_api,
        memecoin_replay_receipt_api=memecoin_replay_receipt_api,
        memecoin_detail_page=memecoin_detail_page,
        make_memecoin_call_api=make_memecoin_call_api,
        close_memecoin_call_api=close_memecoin_call_api,
    )
