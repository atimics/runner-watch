from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field


class SportsPickPayload(BaseModel):
    selection: Literal["home", "away"]
    expected_odds: int | None = Field(default=None, strict=True)


@dataclass(frozen=True)
class SportsRouteDependencies:
    templates: Jinja2Templates
    sports_origin: Callable[[], str]
    app_origin: Callable[[], str]
    enforce_rate: Callable[..., Any]
    product_for_request: Callable[..., Any]
    _conditional_json_response: Callable[..., Any]
    _sports_alpha_data: Callable[..., Any]
    alpha_board_data: Callable[..., Any]
    _public_sports_pulse_data: Callable[..., Any]
    _public_golf_data: Callable[..., Any]
    _public_sports_radar_data: Callable[..., Any]
    sports_alpha: Callable[..., Any]
    sports_slate: Callable[..., Any]
    connection: Callable[..., Any]
    sports_event: Callable[..., Any]
    sports_team_profile: Callable[..., Any]
    sports_player_profile: Callable[..., Any]
    page_context: Callable[..., Any]
    golf_event: Callable[..., Any]
    simple_market_detail: Callable[..., Any]
    golf_market_context: Callable[..., Any]
    _public_screen_data: Callable[..., Any]
    current_user: Callable[..., Any]
    sports_pick_for_user: Callable[..., Any]
    sports_call_reward: Callable[..., Any]
    comments_for_subject: Callable[..., Any]
    daily_report_for_sports_game: Callable[..., Any]
    comment_count_for_subject: Callable[..., Any]
    _flash_provider_ready: Callable[..., Any]
    _flash_report_action: Callable[..., Any]
    latest_commission: Callable[..., Any]
    _sports_report_key: Callable[..., Any]
    require_origin: Callable[..., Any]
    require_user: Callable[..., Any]
    _require_research_route: Callable[..., Any]
    _create_research_commission: Callable[..., Any]
    _enqueue_created_research_report: Callable[..., Any]
    _commission_api_payload: Callable[..., Any]
    run_in_threadpool: Callable[..., Any]
    create_sports_pick: Callable[..., Any]
    _invalidate_public_screen_data: Callable[..., Any]
    _invalidate_sports_alpha_data: Callable[..., Any]


@dataclass(frozen=True)
class SportsRoutes:
    router: APIRouter
    sports_home: Callable[..., Any]
    sports_radar_page: Callable[..., Any]
    alpha_page: Callable[..., Any]
    alpha_api: Callable[..., Any]
    sports_alpha_page: Callable[..., Any]
    sports_receipts_page: Callable[..., Any]
    sports_receipts_legacy_page: Callable[..., Any]
    sports_pulse_api: Callable[..., Any]
    sports_golf_api: Callable[..., Any]
    sports_radar_api: Callable[..., Any]
    sports_alpha_api: Callable[..., Any]
    sports_stats_api: Callable[..., Any]
    sports_slate_api: Callable[..., Any]
    sports_game_legacy_page: Callable[..., Any]
    sports_team_page: Callable[..., Any]
    sports_player_page: Callable[..., Any]
    sports_game_page: Callable[..., Any]
    commission_sports_research_api: Callable[..., Any]
    create_sports_pick_api: Callable[..., Any]


def create_sports_routes(dependencies: SportsRouteDependencies) -> SportsRoutes:
    router = APIRouter()

    @router.get("/sports", response_class=HTMLResponse)
    def sports_home(
        request: Request,
        runner_session: str | None = Cookie(default=None),
        league: str = "all",
        view: str = "signals",
    ) -> RedirectResponse:
        _ = request, runner_session, league, view
        return RedirectResponse(f"{dependencies.sports_origin()}/", status_code=307)

    @router.get("/sports/radar", response_class=HTMLResponse)
    def sports_radar_page(
        request: Request,
        runner_session: str | None = Cookie(default=None),
        league: str = "all",
    ) -> RedirectResponse:
        _ = request, runner_session, league
        return RedirectResponse(f"{dependencies.sports_origin()}/?view=changed", status_code=307)

    @router.get("/alpha", response_class=HTMLResponse)
    def alpha_page(
        request: Request,
        runner_session: str | None = Cookie(default=None),
        league: str = "all",
    ) -> RedirectResponse:
        _ = request, runner_session, league
        return RedirectResponse("/?view=calls", status_code=307)

    @router.get("/api/alpha")
    def alpha_api(
        request: Request,
        league: str = "all",
        limit: int = 24,
    ) -> Response:
        if dependencies.product_for_request(request) == "sports":
            dependencies.enforce_rate(request, "sports-alpha", limit=120, seconds=60)
            return dependencies._conditional_json_response(
                request, dependencies._sports_alpha_data(league, limit)
            )
        dependencies.enforce_rate(request, "alpha", limit=120, seconds=60)
        return dependencies._conditional_json_response(request, dependencies.alpha_board_data())

    @router.get("/sports/alpha", response_class=HTMLResponse)
    def sports_alpha_page(
        request: Request,
        runner_session: str | None = Cookie(default=None),
        league: str = "all",
    ) -> RedirectResponse:
        _ = request, runner_session, league
        return RedirectResponse(f"{dependencies.sports_origin()}/?view=calls", status_code=307)

    @router.get("/receipts", response_class=HTMLResponse)
    def sports_receipts_page(
        request: Request,
        runner_session: str | None = Cookie(default=None),
        league: str = "all",
    ) -> Response:
        _ = runner_session, league
        if dependencies.product_for_request(request) == "sports":
            return RedirectResponse("/?view=calls", status_code=307)
        return RedirectResponse(f"{dependencies.sports_origin()}/?view=calls", status_code=307)

    @router.get("/sports/receipts", response_class=HTMLResponse)
    def sports_receipts_legacy_page(
        request: Request,
        runner_session: str | None = Cookie(default=None),
        league: str = "all",
    ) -> RedirectResponse:
        _ = request, runner_session, league
        return RedirectResponse(f"{dependencies.sports_origin()}/?view=calls", status_code=307)

    @router.get("/api/sports/pulse")
    def sports_pulse_api(
        request: Request,
        league: str = "all",
        view: str = "signals",
        limit: int = 30,
    ) -> Response:
        dependencies.enforce_rate(request, "sports-pulse", limit=120, seconds=60)
        return dependencies._conditional_json_response(
            request,
            dependencies._public_sports_pulse_data(league, view, limit)["pulse"],
        )

    @router.get("/api/sports/golf")
    def sports_golf_api(request: Request, limit: int = 6) -> Response:
        dependencies.enforce_rate(request, "sports-golf", limit=120, seconds=60)
        return dependencies._conditional_json_response(
            request, dependencies._public_golf_data(limit)
        )

    @router.get("/api/sports/radar")
    def sports_radar_api(
        request: Request,
        league: str = "all",
        limit: int = 40,
    ) -> Response:
        dependencies.enforce_rate(request, "sports-radar", limit=120, seconds=60)
        return dependencies._conditional_json_response(
            request,
            dependencies._public_sports_radar_data(league, limit)["radar"],
        )

    @router.get("/api/sports/alpha")
    def sports_alpha_api(
        request: Request,
        league: str = "all",
        limit: int = 24,
    ) -> Response:
        dependencies.enforce_rate(request, "sports-alpha", limit=120, seconds=60)
        return dependencies._conditional_json_response(
            request, dependencies._sports_alpha_data(league, limit)
        )

    @router.get("/api/sports/stats")
    def sports_stats_api(
        request: Request,
        league: str = "all",
        limit: int = 24,
    ) -> JSONResponse:
        dependencies.enforce_rate(request, "sports-stats", limit=120, seconds=60)
        return JSONResponse(dependencies.sports_alpha(league, limit))

    @router.get("/api/slate")
    @router.get("/api/sports/slate")
    def sports_slate_api(
        request: Request,
        league: str = "all",
        limit: int = 80,
    ) -> JSONResponse:
        dependencies.enforce_rate(request, "sports-slate", limit=120, seconds=60)
        return JSONResponse(dependencies.sports_slate(league, limit))

    @router.get("/sports/game/{event_id}", response_class=HTMLResponse)
    def sports_game_legacy_page(event_id: str) -> RedirectResponse:
        return RedirectResponse(_sports_game_location(event_id), status_code=307)

    def _sports_game_location(event_id: str) -> str:
        if event_id.startswith("golf:"):
            with dependencies.connection() as database:
                event = database.execute(
                    "SELECT id FROM sports_golf_events WHERE id=?", (event_id,)
                ).fetchone()
        else:
            event = dependencies.sports_event(event_id)
        if not event:
            raise HTTPException(404, "Game not found")
        canonical_id = quote(str(event["id"]), safe=":")
        return f"{dependencies.sports_origin()}/game/{canonical_id}"

    @router.get("/team/{provider}/{league}/{team_id}", response_class=HTMLResponse)
    def sports_team_page(
        provider: str,
        league: str,
        team_id: str,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> Response:
        profile = dependencies.sports_team_profile(provider, league, team_id)
        if profile is None:
            raise HTTPException(404, "Team not found")
        if (
            dependencies.product_for_request(request) != "sports"
            and dependencies.sports_origin() != dependencies.app_origin()
        ):
            return RedirectResponse(
                f"{dependencies.sports_origin()}{request.url.path}", status_code=307
            )
        return dependencies.templates.TemplateResponse(
            request,
            "sports_entity.html",
            dependencies.page_context(
                request,
                runner_session,
                nav_product="sports",
                screen={"market": "sports", "kind": "profile", "query": ""},
                profile=profile,
            ),
        )

    @router.get("/player/{provider}/{league}/{player_id}", response_class=HTMLResponse)
    def sports_player_page(
        provider: str,
        league: str,
        player_id: str,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> Response:
        profile = dependencies.sports_player_profile(provider, league, player_id)
        if profile is None:
            raise HTTPException(404, "Player not found")
        if (
            dependencies.product_for_request(request) != "sports"
            and dependencies.sports_origin() != dependencies.app_origin()
        ):
            return RedirectResponse(
                f"{dependencies.sports_origin()}{request.url.path}", status_code=307
            )
        return dependencies.templates.TemplateResponse(
            request,
            "sports_entity.html",
            dependencies.page_context(
                request,
                runner_session,
                nav_product="sports",
                screen={"market": "sports", "kind": "profile", "query": ""},
                profile=profile,
            ),
        )

    @router.get("/game/{event_id}", response_class=HTMLResponse)
    def sports_game_page(
        event_id: str,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> Response:
        if (
            dependencies.product_for_request(request) != "sports"
            and dependencies.sports_origin() != dependencies.app_origin()
        ):
            return RedirectResponse(_sports_game_location(event_id), status_code=307)
        if event_id.startswith("golf:"):
            golf = dependencies.golf_event(event_id)
            if golf is None:
                raise HTTPException(404, "Game not found")
            return dependencies.templates.TemplateResponse(
                request,
                "sports_golf_detail.html",
                dependencies.page_context(
                    request,
                    runner_session,
                    nav_product="sports",
                    screen=dependencies.simple_market_detail(
                        "sports",
                        golf,
                        outcome=request.query_params.get("outcome", ""),
                        contract=request.query_params.get("contract", ""),
                    ),
                    golf=golf,
                    golf_context=dependencies.golf_market_context(golf),
                ),
            )
        public_data = dependencies._public_screen_data(
            "sports-game",
            event_id,
            lambda: {"event": dependencies.sports_event(event_id)},
        )
        event = public_data.get("event")
        if not event:
            raise HTTPException(404, "Game not found")
        user = dependencies.current_user(runner_session)
        user_id = str(user["id"]) if user else None
        my_pick = dependencies.sports_pick_for_user(user_id, event_id) if user_id else None
        quote = event.get("paper_odds") or {}
        pick_rewards = {
            "away": dependencies.sports_call_reward(quote.get("away_odds")),
            "home": dependencies.sports_call_reward(quote.get("home_odds")),
        }
        comments = dependencies.comments_for_subject(
            "sports_game", event_id, current_user_id=user_id
        )
        latest_report = dependencies.daily_report_for_sports_game(event_id, user_id)
        sports_path_prefix = ""
        return dependencies.templates.TemplateResponse(
            request=request,
            name="simple_sports_detail.html",
            context=dependencies.page_context(
                request,
                runner_session,
                resolved_user=user,
                event=event,
                my_pick=my_pick,
                pick_rewards=pick_rewards,
                comments=comments,
                comment_count=dependencies.comment_count_for_subject("sports_game", event_id),
                comment_generation_enabled=dependencies._flash_provider_ready(),
                latest_commission=latest_report,
                flash_report=dependencies._flash_report_action(
                    user_id=user_id,
                    latest_report=latest_report,
                    latest_attempt=dependencies.latest_commission(
                        user_id, dependencies._sports_report_key(event_id)
                    )
                    if user_id
                    else None,
                    start_url=f"/api/research/game/{event_id}",
                    login_url=f"/login?next={sports_path_prefix}/game/{event_id}",
                    sports_event=event,
                ),
                active_tab="pulse",
                nav_product="sports",
                sports_path_prefix=sports_path_prefix,
            ),
        )

    @router.post("/api/research/game/{event_id}")
    @router.post("/api/sports/games/{event_id}/research")
    async def commission_sports_research_api(
        event_id: str,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        dependencies.enforce_rate(
            request, "commission-sports-research", limit=20, seconds=3600, subject=user["id"]
        )
        if not dependencies.sports_event(event_id):
            raise HTTPException(404, "Game not found")
        dependencies._require_research_route(str(user["id"]))
        report, created = await dependencies.run_in_threadpool(
            dependencies._create_research_commission,
            str(user["id"]),
            dependencies._sports_report_key(event_id),
        )
        if created:
            report = await dependencies._enqueue_created_research_report(report, str(user["id"]))
        payload = dependencies._commission_api_payload(report, str(user["id"]))
        payload["created"] = created
        return JSONResponse(payload, status_code=202 if payload["status"] == "running" else 200)

    @router.post("/api/calls/game/{event_id}")
    @router.post("/api/picks/{event_id}")
    @router.post("/api/sports/picks/{event_id}")
    def create_sports_pick_api(
        event_id: str,
        payload: SportsPickPayload,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        dependencies.enforce_rate(
            request, "sports-pick", limit=20, seconds=60, subject=str(user["id"])
        )
        try:
            pick = dependencies.create_sports_pick(
                str(user["id"]),
                event_id,
                payload.selection,
                **(
                    {"expected_odds": payload.expected_odds}
                    if payload.expected_odds is not None
                    else {}
                ),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        dependencies._invalidate_public_screen_data("sports-game", event_id)
        dependencies._invalidate_sports_alpha_data()
        if pick.get("caller_handle"):
            dependencies._invalidate_public_screen_data("caller", str(pick["caller_handle"]))
        return JSONResponse(pick, status_code=201)

    return SportsRoutes(
        router=router,
        sports_home=sports_home,
        sports_radar_page=sports_radar_page,
        alpha_page=alpha_page,
        alpha_api=alpha_api,
        sports_alpha_page=sports_alpha_page,
        sports_receipts_page=sports_receipts_page,
        sports_receipts_legacy_page=sports_receipts_legacy_page,
        sports_pulse_api=sports_pulse_api,
        sports_golf_api=sports_golf_api,
        sports_radar_api=sports_radar_api,
        sports_alpha_api=sports_alpha_api,
        sports_stats_api=sports_stats_api,
        sports_slate_api=sports_slate_api,
        sports_game_legacy_page=sports_game_legacy_page,
        sports_team_page=sports_team_page,
        sports_player_page=sports_player_page,
        sports_game_page=sports_game_page,
        commission_sports_research_api=commission_sports_research_api,
        create_sports_pick_api=create_sports_pick_api,
    )
