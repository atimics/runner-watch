from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from runner_web.market_actors import (
    market_actor_comment_budget,
    market_actor_detail,
    record_market_actor_comment,
)
from runner_web.wallet_registry import WALLET_ID, register_chain
from runner_web.wallet_registry import register_people as register_wallet_people
from runner_web.wallet_registry import register_person as register_wallet_person
from runner_web.wallet_registry import wallet as resolve_wallet

# Stocks given a price and score on an entity page, newest filing first.
ENTITY_ENRICHED = 60


@dataclass(frozen=True)
class WalletRouteDependencies:
    enforce_rate: Callable[..., None]
    page_context: Callable[..., dict[str, Any]]
    require_origin: Callable[[Request], None]
    require_user: Callable[[str | None], dict[str, Any]]
    templates: Jinja2Templates
    public_screen_data: Callable[..., Any]
    conditional_json_response: Callable[[Request, Any], Response]
    clean_ticker: Callable[[str], str]
    direct_ticker_item: Callable[..., dict[str, Any] | None]
    openrouter_api_key: Callable[[], str]
    iso: Callable[..., str]
    generate_market_actor_comment_text: Callable[..., tuple[str, str]]
    connection: Callable[[], Any]
    generate_actor_portrait: Callable[..., Any]
    portrait_for_actor: Callable[[str], dict[str, Any] | None]
    market_actor_map: Callable[[str], dict[str, Any]]
    run_in_threadpool: Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class WalletRoutes:
    router: APIRouter
    market_actors_api: Callable[..., Any]
    stock_ticker_map_api: Callable[..., Any]
    stock_cluster_worth_api: Callable[..., Any]
    stock_person_connections_api: Callable[..., Any]
    stock_wallet_page_legacy: Callable[..., Any]
    wallet_page: Callable[..., Any]
    wallet_filings_api: Callable[..., Any]
    wallet_events_api: Callable[..., Any]
    market_actor_api: Callable[..., Any]
    market_actor_portrait_api: Callable[..., Any]
    create_market_actor_comment_api: Callable[..., Any]


def create_wallet_routes(dependencies: WalletRouteDependencies) -> WalletRoutes:
    router = APIRouter()

    @router.get("/wallets", response_class=HTMLResponse)
    def onchain_wallet_list(
        request: Request,
        q: str = Query(default="", max_length=80),
        runner_session: str | None = Cookie(default=None),
    ) -> Response:
        from runner_web.helius_discovery import _address
        from runner_web.market_screens import listing
        from runner_web.onchain_wallets import wallet_catalog

        dependencies.enforce_rate(request, "chain-wallet-list", limit=60, seconds=60)
        if q.strip():
            try:
                address = _address(q.strip())
            except ValueError:
                address = None
            if address:
                return RedirectResponse("/wallet/" + register_chain(address), status_code=303)
        return dependencies.templates.TemplateResponse(
            request,
            "onchain_wallet_list.html",
            dependencies.page_context(
                request,
                runner_session,
                nav_product="memecoins",
                screen=listing("memecoins", []),
                catalog=wallet_catalog(q),
            ),
        )

    @router.get("/wallets/solana/{address}")
    def solana_wallet_redirect(address: str, request: Request) -> Response:
        dependencies.enforce_rate(request, "chain-wallet-open", limit=60, seconds=60)
        try:
            wallet_id = register_chain(address)
        except ValueError as exc:
            raise HTTPException(400, "Enter a valid Solana wallet address") from exc
        return RedirectResponse("/wallet/" + wallet_id, status_code=303)

    @router.get("/api/wallets/{wallet_id}/pnl")
    def wallet_pnl_api(wallet_id: str, request: Request, summary: bool = False) -> Response:
        from runner_web.onchain_wallets import saved_wallet

        dependencies.enforce_rate(request, "chain-wallet-pnl", limit=60, seconds=60)
        resolved = resolve_wallet(wallet_id)
        if not resolved or resolved["kind"] != "solana":
            raise HTTPException(404, "Solana wallet not found")
        snapshot = saved_wallet(resolved["address"])
        if summary:
            snapshot = {
                key: snapshot.get(key) for key in ("status", "error", "updated_at", "backfill")
            }
        return JSONResponse(snapshot)

    @router.post("/api/wallets/{wallet_id}/refresh")
    def wallet_pnl_refresh_api(wallet_id: str, request: Request) -> Response:
        from runner_web.onchain_wallets import refresh_wallet

        dependencies.require_origin(request)
        dependencies.enforce_rate(request, "chain-wallet-refresh", limit=3, seconds=60)
        resolved = resolve_wallet(wallet_id)
        if not resolved or resolved["kind"] != "solana":
            raise HTTPException(404, "Solana wallet not found")
        return JSONResponse(refresh_wallet(resolved["address"]))

    @router.get("/api/market-actors")
    def market_actors_api(request: Request, domain: str = "stock") -> dict[str, Any]:
        dependencies.enforce_rate(request, "market-map", limit=120, seconds=60)
        return dependencies.market_actor_map(domain)

    @router.get("/api/stocks/{ticker}/map")
    def stock_ticker_map_api(
        ticker: str,
        request: Request,
        cursor: str | None = Query(default=None, max_length=1024),
    ) -> dict[str, Any]:
        from runner_web.stock_map import ticker_map

        dependencies.enforce_rate(request, "stock-ticker-map", limit=120, seconds=60)
        normalized = dependencies.clean_ticker(ticker)
        try:
            # The wallet page asks for the same holder page as the stock map, so keep
            # it warm in the shared cache and let the browser reuse it too.
            payload = dependencies.public_screen_data(
                "ticker-map",
                f"{normalized}:{cursor or 'first'}",
                lambda: ticker_map(normalized, cursor),
            )
        except ValueError as exc:
            raise HTTPException(400, "Invalid map cursor") from exc
        register_wallet_people(payload.get("events") or [], normalized)
        return dependencies.conditional_json_response(request, payload)

    @router.get("/api/stocks/{ticker}/cluster-worth")
    def stock_cluster_worth_api(ticker: str, request: Request) -> Response:
        from runner_web.cluster_worth import cluster_worth

        dependencies.enforce_rate(request, "stock-cluster-worth", limit=60, seconds=60)
        normalized = dependencies.clean_ticker(ticker)
        payload = dependencies.public_screen_data(
            "cluster-worth", normalized, lambda: cluster_worth(normalized), ttl_seconds=300
        )
        return dependencies.conditional_json_response(request, payload)

    @router.get("/api/stocks/{ticker}/map/connections")
    def stock_person_connections_api(
        ticker: str,
        request: Request,
        person_id: str = Query(max_length=32),
        cursor: str | None = Query(default=None, max_length=1024),
    ) -> dict[str, Any]:
        from runner_web.stock_map import person_connections

        dependencies.enforce_rate(request, "stock-person-connections", limit=120, seconds=60)
        normalized = dependencies.clean_ticker(ticker)
        try:
            payload = dependencies.public_screen_data(
                "person-connections",
                f"{normalized}:{person_id}:{cursor or 'first'}",
                lambda: person_connections(normalized, person_id, cursor),
            )
        except ValueError as exc:
            raise HTTPException(400, "Invalid connection request") from exc
        register_wallet_people(payload.get("events") or [], normalized)
        return dependencies.conditional_json_response(request, payload)

    @router.get("/wallets/stocks/{ticker}/{person_id}", response_class=HTMLResponse)
    def stock_wallet_page_legacy(
        ticker: str,
        person_id: str,
        request: Request,
    ) -> Response:
        """A wallet used to be addressed through a stock; send it to the wallet path."""

        _ = request
        wallet_id = register_wallet_person(person_id, dependencies.clean_ticker(ticker))
        # Only a minted wallet id can be a redirect target, so nothing tainted from
        # the request reaches the Location header.
        if wallet_id is None or not WALLET_ID.fullmatch(wallet_id):
            raise HTTPException(404, "Wallet not found")
        return RedirectResponse(f"/wallet/{wallet_id}", status_code=301)

    @router.get("/wallet/{wallet_id}", response_class=HTMLResponse)
    def wallet_page(
        wallet_id: str,
        request: Request,
        cursor: str | None = Query(default=None, max_length=1024),
        holdings_page: int = Query(default=1, ge=1, le=200),
        runner_session: str | None = Cookie(default=None),
    ) -> HTMLResponse:
        """A wallet stands on its own: no stock in the path, whatever it identifies."""

        from runner_web.entity_view import entity_view
        from runner_web.market_screens import listing
        from runner_web.stock_map import entity_events, person_connections

        dependencies.enforce_rate(request, "stock-wallet", limit=60, seconds=60)
        resolved = resolve_wallet(wallet_id)
        if resolved and resolved["kind"] == "solana":
            from runner_web.onchain_wallets import holdings_page as wallet_holdings_page
            from runner_web.onchain_wallets import saved_wallet

            return dependencies.templates.TemplateResponse(
                request,
                "onchain_wallet.html",
                dependencies.page_context(
                    request,
                    runner_session,
                    nav_product="memecoins",
                    screen=listing("memecoins", []),
                    wallet_id=wallet_id,
                    wallet=wallet_holdings_page(saved_wallet(resolved["address"]), holdings_page),
                ),
            )
        person_id = str(resolved.get("person_id") or "") if resolved else ""
        if not resolved or not person_id:
            raise HTTPException(404, "Wallet not found")
        scope = str(resolved.get("scope") or "")
        try:
            connections = person_connections(scope, person_id, cursor)
        except ValueError as exc:
            raise HTTPException(400, "Invalid wallet request") from exc
        events = connections["events"]
        register_wallet_people(events, scope)
        person = next(
            (entry for event in events for entry in event["people"] if entry["id"] == person_id),
            {"name": "Wallet", "id": person_id},
        )
        # The map and the tracked worth read the whole record (up to a bound), not
        # the page of events listed below; older events only extend the list.
        try:
            map_events, complete = entity_events(
                scope, person_id, connections if not cursor else None
            )
        except ValueError as exc:
            raise HTTPException(400, "Invalid wallet request") from exc
        if cursor:
            map_events = list({event["id"]: event for event in [*events, *map_events]}.values())
        newest = sorted(map_events, key=lambda event: event["filed_at"], reverse=True)
        stocks = list(dict.fromkeys(event["ticker"] for event in newest))
        # Prices and scores are looked up for the newest stocks only; the rest
        # stay on the map as holdings without a value.
        items = [
            (dependencies.direct_ticker_item(symbol, []) if index < ENTITY_ENRICHED else None)
            or {"ticker": symbol}
            for index, symbol in enumerate(stocks)
        ]
        screen = listing("stocks", items)
        return dependencies.templates.TemplateResponse(
            request,
            "stock_wallet.html",
            dependencies.page_context(
                request,
                runner_session,
                nav_product="runners",
                screen=screen,
                wallet=person,
                wallet_events=events,
                wallet_cursor=connections["next_cursor"],
                wallet_ticker=scope,
                wallet_id=wallet_id,
                entity={**entity_view(map_events, items, person_id), "complete": complete},
            ),
        )

    @router.get("/api/wallets/{wallet_id}/filings")
    def wallet_filings_api(
        wallet_id: str,
        request: Request,
        cursor: str | None = Query(default=None, max_length=1024),
    ) -> Response:
        """The next page of a wallet's filings, rendered by the same partial as the page."""

        resolved = resolve_wallet(wallet_id)
        person_id = str(resolved.get("person_id") or "") if resolved else ""
        if not resolved or not person_id:
            raise HTTPException(404, "Wallet not found")
        return _wallet_filings_response(
            request, str(resolved.get("scope") or ""), person_id, cursor
        )

    @router.get("/api/wallets/stocks/{ticker}/{person_id}/events")
    def wallet_events_api(
        ticker: str,
        person_id: str,
        request: Request,
        cursor: str | None = Query(default=None, max_length=1024),
    ) -> Response:
        """The next page of a wallet's filings, rendered by the same partial the page
        uses so the appended rows are identical."""

        dependencies.enforce_rate(request, "stock-wallet-events", limit=120, seconds=60)
        return _wallet_filings_response(
            request, dependencies.clean_ticker(ticker), person_id, cursor
        )

    def _wallet_filings_response(
        request: Request, ticker: str, person_id: str, cursor: str | None
    ) -> Response:
        from runner_web.stock_map import person_connections

        try:
            connections = person_connections(ticker, person_id, cursor)
        except ValueError as exc:
            raise HTTPException(400, "Invalid wallet request") from exc
        events = connections["events"]
        person = next(
            (entry for event in events for entry in event["people"] if entry["id"] == person_id),
            {"name": "Wallet", "id": person_id},
        )
        partial = dependencies.templates.get_template("_wallet_event.html")
        return dependencies.conditional_json_response(
            request,
            {
                "html": "".join(partial.render(event=event, wallet=person) for event in events),
                "next_cursor": connections["next_cursor"],
                "count": len(events),
            },
        )

    @router.get("/api/market-actors/{actor_id}")
    def market_actor_api(actor_id: str, request: Request) -> dict[str, Any]:
        dependencies.enforce_rate(request, "market-map", limit=120, seconds=60)
        detail = market_actor_detail(actor_id)
        if detail is None:
            raise HTTPException(404, "Market actor not found")
        return detail

    @router.get("/api/market-actors/{actor_id}/portrait")
    def market_actor_portrait_api(
        actor_id: str, request: Request, cached: bool = False
    ) -> Response:
        dependencies.enforce_rate(request, "market-actor-portrait", limit=60, seconds=60)
        existing = dependencies.portrait_for_actor(actor_id)
        if existing is None and not cached:
            dependencies.generate_actor_portrait(
                actor_id, api_key=dependencies.openrouter_api_key()
            )
            existing = dependencies.portrait_for_actor(actor_id)
        if existing is None:
            raise HTTPException(404, "No portrait for this character")
        return Response(
            content=existing["bytes"],
            media_type=existing["content_type"],
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @router.post("/api/market-actors/{actor_id}/comment")
    async def create_market_actor_comment_api(
        actor_id: str,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        detail = market_actor_detail(actor_id)
        if detail is None:
            raise HTTPException(404, "Market actor not found")
        if not dependencies.openrouter_api_key():
            raise HTTPException(503, "AI comments are temporarily unavailable.")
        await dependencies.run_in_threadpool(
            dependencies.enforce_rate,
            request,
            "market-actor-comment",
            limit=10,
            seconds=3600,
            subject=str(user["id"]),
        )
        budget = market_actor_comment_budget(actor_id)
        if not budget["allowed"]:
            raise HTTPException(429, "This character has posted enough for now.")
        actor = detail["actor"]
        body, model = await dependencies.run_in_threadpool(
            dependencies.generate_market_actor_comment_text,
            actor_id,
            avatar=actor["avatar"],
        )
        comment_id = secrets.token_urlsafe(10)
        primary = detail["evidence"][0]["subject_key"] if detail["evidence"] else actor_id
        with dependencies.connection() as db:
            db.execute(
                """
                INSERT INTO market_actor_comments(
                    id,actor_id,subject_key,body,generation_model,created_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (comment_id, actor_id, str(primary), body, model, dependencies.iso()),
            )
        record_market_actor_comment()
        updated = market_actor_detail(actor_id) or {"comments": []}
        return JSONResponse(
            {
                "comment": updated["comments"][0] if updated["comments"] else None,
                "budget": market_actor_comment_budget(actor_id),
            },
            status_code=201,
        )

    return WalletRoutes(
        router=router,
        market_actors_api=market_actors_api,
        stock_ticker_map_api=stock_ticker_map_api,
        stock_cluster_worth_api=stock_cluster_worth_api,
        stock_person_connections_api=stock_person_connections_api,
        stock_wallet_page_legacy=stock_wallet_page_legacy,
        wallet_page=wallet_page,
        wallet_filings_api=wallet_filings_api,
        wallet_events_api=wallet_events_api,
        market_actor_api=market_actor_api,
        market_actor_portrait_api=market_actor_portrait_api,
        create_market_actor_comment_api=create_market_actor_comment_api,
    )
