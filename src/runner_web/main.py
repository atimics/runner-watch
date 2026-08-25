from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import textwrap
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import Cookie, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AttestationConveyancePreference,
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from runner_watch.models import ScanSettings
from runner_watch.risk import RiskInput, assess_risk
from runner_watch.scanner import RunnerScanner
from runner_watch.universe import penny_runner_universe
from runner_web import db as runner_db
from runner_web.ai_kol import FLASH, AIKol, actor_snapshot
from runner_web.base_rates import (
    BASE_RATE_LOOKBACK_DAYS,
    MATCH_TOLERANCE_MINUTES,
    MIN_BASE_RATE_SAMPLES,
    matched_market_base_rates,
)
from runner_web.collection import recording_market_data
from runner_web.db import connection, init_db
from runner_web.ingestion import ingestion_status, record_source_fetch
from runner_web.intelligence import record_edgar_error, refresh_edgar
from runner_web.issuer_risk import issuer_risk_contexts
from runner_web.kol import (
    calls_for_ticker,
    calls_for_tickers,
    kol_status,
    predictor_scorecards,
    publish_calls_for_scan,
    refresh_kol_calls,
)
from runner_web.market_clock import market_clock
from runner_web.outcomes import record_outcome_error, refresh_outcomes, refresh_scan_outcomes
from runner_web.pseudonyms import pseudonym_candidate
from runner_web.ranker import (
    FEATURE_SCHEMA_VERSION,
    predict_and_store,
    ranker_status,
    train_shadow_ranker,
)
from runner_web.research_context import build_research_context
from runner_web.source_workers import (
    apewisdom_source_worker,
    discovery_source_worker,
    trading_halt_worker,
)
from runner_web.topics import TopicHub, TopicPolicy, TopicSnapshot, TopicUpdate

LOG = logging.getLogger(__name__)

APP_ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:8080").rstrip("/")
RP_ID = os.getenv("RP_ID", "localhost")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
ROOT = Path(os.getenv("RUNNER_ROOT", Path.cwd()))
SESSION_COOKIE = "runner_session"
VISITOR_COOKIE = "runner_visitor"
TICKER_RE = re.compile(r"^[A-Z0-9.-]{1,12}$")
VISITOR_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
AI_REPORT_MODEL = os.getenv("AI_REPORT_MODEL", "gpt-5.6")
AI_REPORT_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENROUTER_RESEARCH_OUTPUT_TOKENS = max(
    4_000, int(os.getenv("OPENROUTER_RESEARCH_OUTPUT_TOKENS", "12000"))
)
SCAN_MODES = {
    "penny": {
        "label": "Penny stocks",
        "min_price": 0.20,
        "max_price": 5.00,
        "crash_only": False,
    },
    "low_price": {
        "label": "Low-priced small caps",
        "min_price": 0.20,
        "max_price": 20.00,
        "crash_only": False,
    },
    "crash": {
        "label": "60% crash recovery",
        "min_price": 0.20,
        "max_price": 20.00,
        "crash_only": True,
    },
}
SCAN_CACHE: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
SCAN_LOCK = threading.Lock()
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMITS: dict[str, list[datetime]] = {}
EASTERN = ZoneInfo("America/New_York")
BACKGROUND_SCAN_INTERVAL_SECONDS = max(
    120, int(os.getenv("BACKGROUND_SCAN_INTERVAL_SECONDS", "180"))
)
PULSE_NOTIFICATION_WINDOW = timedelta(hours=12)
PULSE_CACHE_TTL_SECONDS = max(
    5.0, float(os.getenv("PULSE_CACHE_TTL_SECONDS", "60"))
)
PULSE_DATA_LOCK = threading.Lock()
PULSE_DATA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
PULSE_DATA_REFRESHING: set[str] = set()
RADAR_CACHE_TTL_SECONDS = max(
    5.0, float(os.getenv("RADAR_CACHE_TTL_SECONDS", "60"))
)
RADAR_DATA_LOCK = threading.Lock()
RADAR_DATA_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
RADAR_DATA_REFRESHING: set[str] = set()
ALPHA_CACHE_TTL_SECONDS = max(
    5.0, float(os.getenv("ALPHA_CACHE_TTL_SECONDS", "60"))
)
ALPHA_DATA_LOCK = threading.Lock()
ALPHA_DATA_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
ALPHA_DATA_REFRESHING: set[str] = set()
RESEARCH_JOB_QUEUE: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
CHART_TOPIC_POLICY = TopicPolicy(
    ttl_seconds=180,
    minimum_refresh_seconds=30,
    maximum_stale_seconds=15 * 60,
    keep_last_good=True,
)
# Chart points already live in market_bars. Persisting the derived cache wrote one
# SQLite transaction per ticker on every refresh, which is very slow on a Fly volume.
MARKET_TOPICS = TopicHub()


def _static_version() -> str:
    digest = hashlib.sha256()
    static_root = ROOT / "web" / "static"
    for asset in sorted(static_root.iterdir()):
        if asset.is_file():
            digest.update(asset.name.encode())
            digest.update(asset.read_bytes())
    return digest.hexdigest()[:12]


STATIC_VERSION = _static_version()


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_db()
    _fail_orphaned_research_jobs()
    tasks = [
        asyncio.create_task(edgar_worker()),
        asyncio.create_task(trading_halt_worker()),
        asyncio.create_task(discovery_source_worker()),
        asyncio.create_task(apewisdom_source_worker()),
        asyncio.create_task(outcome_worker()),
        asyncio.create_task(kol_worker()),
        asyncio.create_task(scan_collection_worker()),
        asyncio.create_task(alpha_report_worker()),
        asyncio.create_task(research_job_worker()),
        asyncio.create_task(request_cache_warmer()),
    ]
    application.state.worker_tasks = tasks
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Runner Watch", docs_url=None, redoc_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=str(ROOT / "web" / "templates"))
templates.env.globals["static_version"] = STATIC_VERSION
app.mount("/static", StaticFiles(directory=str(ROOT / "web" / "static")), name="static")


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat()


def worker_state(key: str, value: str) -> None:
    timestamp = iso()
    with connection() as db:
        db.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, value, timestamp),
        )


def prune_storage() -> None:
    """Keep the beta database bounded without removing scored snapshots or receipts."""

    with connection() as db:
        previous = db.execute(
            "SELECT updated_at FROM worker_state WHERE key='storage_last_prune'"
        ).fetchone()
        if previous and str(previous["updated_at"]) > iso(now() - timedelta(hours=23)):
            return
        bars_deleted = db.execute(
            "DELETE FROM market_bars WHERE last_collected_at<?",
            (iso(now() - timedelta(days=60)),),
        ).rowcount
        documents_deleted = db.execute(
            "DELETE FROM source_documents WHERE last_collected_at<?",
            (iso(now() - timedelta(days=365)),),
        ).rowcount
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (iso(),))
        db.execute("DELETE FROM auth_challenges WHERE expires_at<=?", (iso(),))
        db.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (
                "storage_last_prune",
                json.dumps({"market_bars": bars_deleted, "source_documents": documents_deleted}),
                iso(),
            ),
        )


def row_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin or origin.rstrip("/") != APP_ORIGIN:
        raise HTTPException(403, "Origin check failed")


def enforce_rate(
    request: Request,
    scope: str,
    *,
    limit: int,
    seconds: int,
    subject: str | None = None,
) -> None:
    client = request.client.host if request.client else "unknown"
    key = f"{scope}:{subject or client}"
    cutoff = now() - timedelta(seconds=seconds)
    with RATE_LIMIT_LOCK:
        recent = [stamp for stamp in RATE_LIMITS.get(key, []) if stamp > cutoff]
        if len(recent) >= limit:
            raise HTTPException(429, "Too many requests. Please wait and try again.")
        recent.append(now())
        RATE_LIMITS[key] = recent
        if len(RATE_LIMITS) > 5_000:
            stale = [
                name for name, stamps in RATE_LIMITS.items() if not stamps or stamps[-1] <= cutoff
            ]
            for name in stale[:1_000]:
                RATE_LIMITS.pop(name, None)


def current_user(session_token: str | None) -> dict[str, Any] | None:
    if not session_token:
        return None
    with connection() as db:
        row = db.execute(
            """
            SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
            WHERE s.token_hash=? AND s.expires_at>?
            """,
            (token_hash(session_token), iso()),
        ).fetchone()
    return row_dict(row)


def require_user(session_token: str | None) -> dict[str, Any]:
    user = current_user(session_token)
    if not user:
        raise HTTPException(401, "Passkey login required")
    return user


def profile_id(request: Request, user: dict[str, Any] | None = None) -> str:
    if user:
        return f"u:{user['id']}"
    return f"v:{request.state.visitor_id}"


def claim_visitor_profile(request: Request, user: dict[str, Any]) -> str:
    target = profile_id(request, user)
    source = profile_id(request)
    if source == target:
        return target
    with connection() as db:
        db.execute(
            """
            INSERT INTO ticker_hearts(profile_id,ticker,active,created_at,updated_at)
            SELECT ?,ticker,active,created_at,updated_at FROM ticker_hearts
            WHERE profile_id=?
            ON CONFLICT(profile_id,ticker) DO UPDATE SET
                active=MAX(ticker_hearts.active,excluded.active),
                updated_at=MAX(ticker_hearts.updated_at,excluded.updated_at)
            """,
            (target, source),
        )
        db.execute("DELETE FROM ticker_hearts WHERE profile_id=?", (source,))
        db.execute(
            """
            INSERT INTO ticker_reactions(profile_id,ticker,reaction,created_at,updated_at)
            SELECT ?,ticker,reaction,created_at,updated_at FROM ticker_reactions
            WHERE profile_id=?
            ON CONFLICT(profile_id,ticker) DO NOTHING
            """,
            (target, source),
        )
        db.execute("DELETE FROM ticker_reactions WHERE profile_id=?", (source,))
        db.execute("UPDATE activity_events SET profile_id=? WHERE profile_id=?", (target, source))
        db.execute(
            """
            INSERT INTO radar_seen(profile_id,ticker,last_seen_at)
            SELECT ?,ticker,last_seen_at FROM radar_seen WHERE profile_id=?
            ON CONFLICT(profile_id,ticker) DO UPDATE SET
                last_seen_at=MAX(radar_seen.last_seen_at,excluded.last_seen_at)
            """,
            (target, source),
        )
        db.execute("DELETE FROM radar_seen WHERE profile_id=?", (source,))
        db.execute(
            """
            INSERT INTO pulse_profile_state(
                profile_id,ticker,entered_at,first_seen_at,last_seen_at,
                inspected_at,notified_at
            )
            SELECT ?,ticker,entered_at,first_seen_at,last_seen_at,
                   inspected_at,notified_at
            FROM pulse_profile_state WHERE profile_id=?
            ON CONFLICT(profile_id,ticker,entered_at) DO UPDATE SET
                first_seen_at=COALESCE(
                    pulse_profile_state.first_seen_at,excluded.first_seen_at
                ),
                last_seen_at=CASE
                    WHEN pulse_profile_state.last_seen_at IS NULL
                        THEN excluded.last_seen_at
                    WHEN excluded.last_seen_at IS NULL
                        THEN pulse_profile_state.last_seen_at
                    ELSE MAX(pulse_profile_state.last_seen_at,excluded.last_seen_at)
                END,
                inspected_at=COALESCE(
                    pulse_profile_state.inspected_at,excluded.inspected_at
                ),
                notified_at=COALESCE(
                    pulse_profile_state.notified_at,excluded.notified_at
                )
            """,
            (target, source),
        )
        db.execute("DELETE FROM pulse_profile_state WHERE profile_id=?", (source,))
    return target


def create_session(user_id: str, response: JSONResponse) -> None:
    raw_token = secrets.token_urlsafe(32)
    created = now()
    with connection() as db:
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (iso(created),))
        db.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (
                token_hash(raw_token),
                user_id,
                iso(created),
                iso(created + timedelta(days=30)),
            ),
        )
    response.set_cookie(
        SESSION_COOKIE,
        raw_token,
        max_age=30 * 24 * 3600,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def save_challenge(kind: str, challenge: bytes, user_id: str | None = None) -> str:
    token = secrets.token_urlsafe(24)
    with connection() as db:
        db.execute("DELETE FROM auth_challenges WHERE expires_at<=?", (iso(),))
        db.execute(
            """
            INSERT INTO auth_challenges(token,user_id,kind,challenge,expires_at)
            VALUES(?,?,?,?,?)
            """,
            (token, user_id, kind, challenge, iso(now() + timedelta(minutes=5))),
        )
    return token


def take_challenge(token: str, kind: str) -> dict[str, Any]:
    with connection() as db:
        row = db.execute(
            "SELECT * FROM auth_challenges WHERE token=? AND kind=? AND expires_at>?",
            (token, kind, iso()),
        ).fetchone()
        if row:
            db.execute("DELETE FROM auth_challenges WHERE token=?", (token,))
    if not row:
        raise HTTPException(400, "This passkey request expired. Please try again.")
    return dict(row)


def page_context(request: Request, session_token: str | None, **extra: Any) -> dict[str, Any]:
    user = current_user(session_token)
    return {
        "request": request,
        "user": user,
        "is_subscriber": bool(user and user.get("plan") == "subscriber"),
        "openrouter_storage_id": (
            hashlib.sha256(str(user["id"]).encode()).hexdigest()[:16] if user else "guest"
        ),
        "app_origin": APP_ORIGIN,
        "market_clock": market_clock(),
        "flash": actor_snapshot(),
        **extra,
    }


async def edgar_worker() -> None:
    while True:
        try:
            await run_in_threadpool(refresh_edgar)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record_edgar_error(exc)
        await asyncio.sleep(45)


async def outcome_worker() -> None:
    await asyncio.sleep(75)
    while True:
        try:
            await run_in_threadpool(refresh_outcomes)
            scan_result = await run_in_threadpool(refresh_scan_outcomes)
            await run_in_threadpool(refresh_kol_calls, latest_prices={})
            if scan_result["barrier_labels_added"]:
                await run_in_threadpool(train_shadow_ranker)
            await run_in_threadpool(prune_storage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record_outcome_error(exc)
        await asyncio.sleep(600)


async def kol_worker() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            result = await run_in_threadpool(refresh_kol_calls)
            worker_state("kol_last_refresh", json.dumps(result, separators=(",", ":")))
            worker_state("kol_last_error", "")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            worker_state("kol_last_error", str(exc)[:500])
        await asyncio.sleep(60)


async def scan_collection_worker() -> None:
    await asyncio.sleep(15)
    while True:
        eastern_now = now().astimezone(EASTERN)
        market_window = clock_time(4) <= eastern_now.time().replace(tzinfo=None) < clock_time(20)
        with connection() as db:
            has_saved_scan = db.execute("SELECT 1 FROM scan_runs LIMIT 1").fetchone() is not None
        should_scan = (eastern_now.weekday() < 5 and market_window) or not has_saved_scan
        if should_scan:
            try:
                result = await run_in_threadpool(run_scan, "penny")
                worker_state("background_scan_last_run", str(result.get("scan_run_id") or "cached"))
                worker_state("background_scan_last_error", "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                worker_state("background_scan_last_error", str(exc)[:500])
        await asyncio.sleep(BACKGROUND_SCAN_INTERVAL_SECONDS)


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Response:
    started = time.perf_counter()
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    raw_visitor = request.cookies.get(VISITOR_COOKIE, "")
    visitor_id = raw_visitor if VISITOR_RE.fullmatch(raw_visitor) else secrets.token_urlsafe(24)
    request.state.visitor_id = visitor_id
    response = await call_next(request)
    if visitor_id != raw_visitor:
        response.set_cookie(
            VISITOR_COOKIE,
            visitor_id,
            max_age=365 * 24 * 3600,
            httponly=True,
            secure=COOKIE_SECURE,
            samesite="lax",
            path="/",
        )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if COOKIE_SECURE:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self' https://openrouter.ai; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.headers["Server-Timing"] = f"app;dur={elapsed_ms:.1f}"
    if elapsed_ms >= 500:
        LOG.info(
            "slow_request method=%s path=%s status=%s duration_ms=%.1f",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
    return response


class PasskeyFinish(BaseModel):
    flow_token: str
    credential: dict[str, Any]


class PublishSignal(BaseModel):
    snapshot_id: str
    thesis: str = Field(min_length=8, max_length=500)
    horizon: str = Field(pattern="^(intraday|swing|watch)$")
    invalidation: str = Field(min_length=3, max_length=240)
    disclosure: str = Field(min_length=3, max_length=240)


class ReportSignal(BaseModel):
    reason: str = Field(min_length=3, max_length=240)


class ActivityPayload(BaseModel):
    ticker: str
    event_type: str = Field(pattern="^(view|dwell|share)$")
    seconds: int = Field(default=0, ge=0, le=3600)


class TickerCommentPayload(BaseModel):
    body: str = Field(min_length=1, max_length=280)


class PulseAttentionItem(BaseModel):
    ticker: str = Field(min_length=1, max_length=12)
    entered_at: str = Field(min_length=10, max_length=64)


class PulseAttentionPayload(BaseModel):
    entries: list[PulseAttentionItem] = Field(min_length=1, max_length=50)


@app.get("/health")
def health() -> dict[str, Any]:
    with connection() as db:
        db.execute("SELECT 1").fetchone()
        states = {
            row["key"]: {"value": row["value"], "updated_at": row["updated_at"]}
            for row in db.execute("SELECT key,value,updated_at FROM worker_state").fetchall()
        }
        latest_scan = db.execute("SELECT MAX(captured_at) FROM scan_runs").fetchone()[0]
    tasks = getattr(app.state, "worker_tasks", [])
    return {
        "status": "ok" if not tasks or all(not task.done() for task in tasks) else "degraded",
        "database": "ok",
        "workers_running": sum(not task.done() for task in tasks),
        "latest_scan_at": latest_scan,
        "edgar_updated_at": states.get("edgar_last_refresh", {}).get("updated_at"),
        "scan_error": states.get("background_scan_last_error", {}).get("value") or None,
    }


@app.get("/api/ranker/status")
def api_ranker_status() -> dict[str, Any]:
    return ranker_status()


@app.get("/api/kols")
def api_kol_status(request: Request) -> dict[str, Any]:
    enforce_rate(request, "kols", limit=120, seconds=60)
    return kol_status()


@app.get("/api/t/{ticker}/kol-calls")
def api_ticker_kol_calls(request: Request, ticker: str) -> dict[str, Any]:
    enforce_rate(request, "ticker-kol-calls", limit=120, seconds=60)
    normalized = _clean_ticker(ticker)
    return {"ticker": normalized, "calls": calls_for_ticker(normalized)}


@app.get("/api/ingestion/status")
def api_ingestion_status() -> dict[str, Any]:
    return ingestion_status()


def runtime_capabilities() -> dict[str, Any]:
    """Describe what this deployment can do without exposing credentials."""

    ingestion = ingestion_status()
    source_rows = ingestion["sources"]
    sources = {
        f"{row['source']}:{row['feed']}": {
            "title": row["title"],
            "enabled": bool(row["enabled"]),
            "state": row["health"],
            "last_success_at": row.get("last_success_at"),
            "age_seconds": row.get("age_seconds"),
            "review_status": row["review_status"],
        }
        for row in source_rows
    }

    def feature(*keys: str) -> dict[str, Any]:
        matched = [sources[key] for key in keys if key in sources]
        live = [row for row in matched if row["enabled"] and row["state"] == "healthy"]
        enabled = [row for row in matched if row["enabled"]]
        state = (
            "healthy"
            if live
            else "degraded"
            if enabled
            else "disabled"
            if matched
            else "unavailable"
        )
        return {"state": state, "sources": list(keys)}

    ranker = ranker_status()
    model = ranker.get("model")
    tasks = getattr(app.state, "worker_tasks", [])
    failed_workers = sum(task.done() for task in tasks)
    source_problems = sum(
        row["enabled"] and row["health"] in {"failed", "stale"} for row in source_rows
    )
    return {
        "checked_at": ingestion["checked_at"],
        "status": "degraded" if failed_workers or source_problems else "ok",
        "features": {
            "sec_filings": feature("sec:current_filings"),
            "issuer_facts": feature("sec:company_facts"),
            "market_bars": feature("yahoo:market_bars"),
            "news": feature("yahoo:news_search", "gdelt:news_search"),
            "public_social": feature(
                "apewisdom:reddit_trends", "bluesky:social_search"
            ),
            "trading_halts": feature("nasdaq_trader:trade_halts"),
        },
        "analysis": {
            "evidence_gate": {
                "mode": "independent_families",
                "version": 2,
                "required_family": "market",
                "threshold": 3,
                "families": ["market", "primary", "news", "crowd"],
            },
            "market_base_rates": {
                "mode": "empirical_matched_sessions",
                "minimum_samples": MIN_BASE_RATE_SAMPLES,
                "lookback_days": BASE_RATE_LOOKBACK_DAYS,
                "clock_tolerance_minutes": MATCH_TOLERANCE_MINUTES,
            },
            "ranker": {
                "state": "shadow" if model else "learning",
                "model_id": model.get("id") if model else None,
                "feature_schema_version": ranker["feature_schema_version"],
                "barrier_labeled": ranker["barrier_labeled"],
            },
            "research": {
                "openai_available": bool(AI_REPORT_API_KEY),
                "openrouter_available": bool(os.getenv("OPENROUTER_API_KEY")),
                "flash_model": FLASH.model,
            },
        },
        "workers": {
            "running": sum(not task.done() for task in tasks),
            "failed": failed_workers,
        },
        "source_summary": ingestion["summary"],
        "sources": sources,
    }


@app.get("/api/capabilities")
def api_capabilities() -> dict[str, Any]:
    return runtime_capabilities()


@app.get("/api/market-clock")
def api_market_clock(request: Request) -> dict[str, Any]:
    enforce_rate(request, "market-clock", limit=120, seconds=60)
    return market_clock()


@app.get("/community", response_class=HTMLResponse)
def community(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    board = alpha_board_data(profile)
    return templates.TemplateResponse(
        request=request,
        name="community.html",
        context=page_context(
            request,
            runner_session,
            board=board,
            active_tab="alpha",
        ),
    )


def _intelligence_evidence(row: dict[str, Any]) -> dict[str, Any]:
    codes = {code for code in str(row.get("transaction_codes", "")).split(",") if code}
    actor = row.get("actor") or "An insider"
    shares = row.get("transaction_shares")
    price = row.get("transaction_price")
    if "P" in codes:
        row["evidence_label"] = "Insider purchase"
        if shares and price:
            row["evidence_text"] = (
                f"{actor} reported buying {float(shares):,.0f} shares at "
                f"${float(price):,.2f}. Check the stake size and footnotes."
            )
        else:
            row["evidence_text"] = (
                "The Form 4 reports a purchase. Check the stake size and footnotes."
            )
    elif "S" in codes:
        row["evidence_label"] = "Insider sale"
        row["evidence_text"] = (
            f"{actor} reported a sale. Check the filing for plan and ownership context."
        )
    elif row.get("sentiment") == "risk":
        row["evidence_label"] = "Risk filing"
        row["evidence_text"] = "This form may add dilution, supply, or reporting risk."
    elif str(row.get("form", "")).startswith("4"):
        row["evidence_label"] = "Ownership update"
        row["evidence_text"] = "This is an ownership change, not a reported purchase."
    elif str(row.get("form", "")).startswith(("SC 13D", "SC 13G")):
        ownership_pct = row.get("beneficial_ownership_pct")
        row["evidence_label"] = "Large holder filing"
        row["evidence_text"] = (
            f"The filing reports up to {float(ownership_pct):.1f}% ownership. "
            "Intent and filing delay still matter."
            if ownership_pct is not None
            else "The filing reports a large holder. Intent and filing delay still matter."
        )
    else:
        row["evidence_label"] = "New SEC filing"
        row["evidence_text"] = "Open the filing for details."
    return row


def _coin_tone(ticker: str) -> int:
    return sum(ord(character) for character in ticker) % 5


def _pulse_label(row: dict[str, Any]) -> str:
    codes = {code for code in str(row.get("transaction_codes", "")).split(",") if code}
    title = str(row.get("actor_title") or "").lower()
    if "P" in codes:
        return "Form 4 · CEO buy" if "ceo" in title else "Form 4 · insider buy"
    if "S" in codes:
        return "Form 4 · insider sale"
    if row.get("sentiment") == "risk":
        return str(row.get("kind") or "SEC risk filing")
    if str(row.get("form", "")).startswith("4"):
        return "Ownership update"
    return f"{row.get('form', 'SEC')} · new filing"


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_container(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _safe_source_url(value: Any) -> str | None:
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return candidate[:1500]


def _source_label(value: str) -> str:
    parsed = urlparse(value)
    host = parsed.netloc.lower().removeprefix("www.")
    if host in {"sec.gov", "data.sec.gov"}:
        return "SEC filing"
    return host or "Source"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _market_trade_pressure(ticker: str) -> dict[str, Any]:
    with connection() as db:
        rows = db.execute(
            """
            SELECT bar_time,open,high,low,close,volume FROM market_bars
            WHERE source='yahoo' AND ticker=? AND interval='5m'
            ORDER BY bar_time DESC LIMIT 24
            """,
            (ticker,),
        ).fetchall()
    bars = list(reversed(rows))
    estimated_buy = 0.0
    estimated_sell = 0.0
    volumes: list[float] = []
    usable = 0
    for row in bars:
        opening = _number(row["open"])
        high = _number(row["high"])
        low = _number(row["low"])
        close = _number(row["close"])
        volume = _number(row["volume"])
        if None in (opening, high, low, close, volume) or volume <= 0:
            continue
        spread = high - low
        if spread > 0:
            close_location = max(-1.0, min(1.0, (2 * close - high - low) / spread))
        elif close > opening:
            close_location = 1.0
        elif close < opening:
            close_location = -1.0
        else:
            close_location = 0.0
        estimated_buy += volume * (1 + close_location) / 2
        estimated_sell += volume * (1 - close_location) / 2
        volumes.append(volume)
        usable += 1
    total = estimated_buy + estimated_sell
    if total <= 0:
        return {
            "available": False,
            "bar_count": 0,
            "method": "close-location volume proxy",
            "note": "Waiting for 5-minute bars.",
        }
    buy_pct = 100 * estimated_buy / total
    recent = volumes[-6:]
    prior = volumes[-12:-6]
    volume_burst = None
    if recent and prior and sum(prior) > 0:
        volume_burst = (sum(recent) / len(recent)) / (sum(prior) / len(prior))
    label = "Buy pressure" if buy_pct >= 60 else "Sell pressure" if buy_pct <= 40 else "Balanced"
    return {
        "available": True,
        "label": label,
        "buy_pressure_pct": round(buy_pct, 1),
        "sell_pressure_pct": round(100 - buy_pct, 1),
        "delta_volume": round(estimated_buy - estimated_sell),
        "estimated_buy_volume": round(estimated_buy),
        "estimated_sell_volume": round(estimated_sell),
        "volume_burst": round(volume_burst, 2) if volume_burst is not None else None,
        "bar_count": usable,
        "as_of": str(bars[-1]["bar_time"]) if bars else None,
        "method": "close-location volume proxy",
        "note": "Estimate from 5-minute bars, not live order flow.",
    }


def _evidence_gate(
    current: dict[str, Any],
    events: list[dict[str, Any]],
    pressure: dict[str, Any] | None = None,
    external_context: dict[str, Any] | None = None,
    base_rates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    market_checks: list[str] = []
    blockers: list[str] = []
    relative_volume = _number(current.get("relative_volume"))
    recent_relative_volume = _number(current.get("recent_relative_volume"))
    momentum_15m = _number(current.get("momentum_15m_pct"))
    acceleration = _number(current.get("momentum_acceleration_pct"))
    vwap_position = _number(current.get("vwap_position_pct"))
    breakout = _number(current.get("breakout_pct"))
    if relative_volume is not None and relative_volume >= 2:
        market_checks.append("Unusual volume")
    if recent_relative_volume is not None and recent_relative_volume >= 3:
        market_checks.append("Volume burst")
    if momentum_15m is not None and momentum_15m >= 3:
        market_checks.append("15m momentum")
    if acceleration is not None and acceleration >= 0.5:
        market_checks.append("Momentum rising")
    if vwap_position is not None and vwap_position > 0:
        market_checks.append("Above VWAP")
    if breakout is not None and breakout > 0:
        market_checks.append("Above prior high")
    if pressure and pressure.get("available") and pressure.get("buy_pressure_pct", 0) >= 60:
        market_checks.append("Buy pressure")

    positive_primary = current.get("catalyst_sentiment") == "positive" or any(
        event.get("sentiment") == "positive" for event in events
    )
    context = external_context or {}
    news_count = int(context.get("news_count") or current.get("news_count") or 0)
    social_mentions = int(
        context.get("social_mentions") or current.get("external_social_mentions") or 0
    )
    heart_count = int(current.get("heart_count") or 0)

    baseline_mode = str((base_rates or {}).get("mode") or "unavailable")
    notable_metrics = list((base_rates or {}).get("notable_metrics") or [])
    market_confirmed = bool(market_checks) and (
        baseline_mode != "empirical" or bool(notable_metrics)
    )
    crowd_confirmed = social_mentions > 0 or heart_count >= 2
    family_receipts = [
        {
            "family": "market",
            "label": "Market structure",
            "status": "confirmed" if market_confirmed else "not_confirmed",
            "mode": "derived",
            "source": "market bars",
            "evidence": market_checks,
            "base_rate": base_rates,
        },
        {
            "family": "primary",
            "label": "Primary filing",
            "status": "confirmed" if positive_primary else "not_confirmed",
            "mode": "observed",
            "source": "SEC",
            "evidence": ["Positive SEC filing"] if positive_primary else [],
        },
        {
            "family": "news",
            "label": "News coverage",
            "status": "confirmed" if news_count > 0 else "not_confirmed",
            "mode": "observed",
            "source": "news metadata",
            "evidence": [f"{news_count} fresh article{'s' if news_count != 1 else ''}"]
            if news_count > 0
            else [],
        },
        {
            "family": "crowd",
            "label": "Crowd activity",
            "status": "confirmed" if crowd_confirmed else "not_confirmed",
            "mode": "observed",
            "source": "public social and community",
            "evidence": [
                *(
                    [f"{social_mentions} public mention{'s' if social_mentions != 1 else ''}"]
                    if social_mentions > 0
                    else []
                ),
                *(
                    [f"{heart_count} community hearts"]
                    if heart_count >= 2
                    else []
                ),
            ],
        },
    ]
    confirmed_families = [
        receipt for receipt in family_receipts if receipt["status"] == "confirmed"
    ]
    checks = [str(receipt["label"]) for receipt in confirmed_families]
    rug_score = _number(current.get("rug_score"))
    rug_level = str(current.get("rug_level") or "UNKNOWN").upper()
    raw_trade_state = current.get("trade_state")
    trade_state = str(raw_trade_state).upper() if raw_trade_state else "UNKNOWN"
    if bool(current.get("hard_veto")):
        blockers.append("Blocked by risk rule")
    if rug_score is not None and rug_score >= 75:
        blockers.append("Critical risk")
    if trade_state in {"AVOID", "EXIT"}:
        blockers.append(f"State: {trade_state.title()}")
    threshold = 3
    evidence_count = len(confirmed_families)
    if blockers:
        state = "blocked"
    elif market_confirmed and evidence_count >= threshold and trade_state in {
        "TRIGGERED",
        "MANAGE",
        "UNKNOWN",
    }:
        state = "ready"
    elif market_confirmed and evidence_count >= threshold - 1 and trade_state in {
        "ARMED",
        "TRIGGERED",
        "MANAGE",
        "UNKNOWN",
    }:
        state = "near"
    else:
        state = "gathering"
    return {
        "count": min(evidence_count, threshold),
        "threshold": threshold,
        "state": state,
        "checks": checks,
        "blockers": blockers,
        "rug_score": rug_score,
        "rug_level": rug_level,
        "trade_state": trade_state,
        "families": family_receipts,
        "raw_market_checks": market_checks,
        "required_family": "market",
        "base_rates": base_rates,
        "baseline_summary": _baseline_summary(base_rates),
        "summary": (
            "Blocked by risk"
            if state == "blocked"
            else "Confirmed"
            if state == "ready"
            else "Almost ready"
            if state == "near"
            else "Waiting for evidence"
        ),
    }


def _baseline_summary(base_rates: dict[str, Any] | None) -> str | None:
    if not base_rates:
        return None
    metrics = base_rates.get("metrics") or {}
    empirical = [
        receipt
        for receipt in metrics.values()
        if isinstance(receipt, dict) and receipt.get("mode") == "empirical"
    ]
    if not empirical:
        matched = int(base_rates.get("matched_sessions") or 0)
        minimum = int(base_rates.get("minimum_samples") or 0)
        return f"Baseline learning: {matched}/{minimum} matched sessions"
    notable = [receipt for receipt in empirical if receipt.get("notable")]
    if not notable:
        return "Market readings are normal for matched sessions"
    strongest = max(notable, key=lambda receipt: float(receipt.get("percentile") or 0))
    percentile = round(float(strongest["percentile"]) * 100)
    return (
        f"{strongest['label']} is above {percentile}% of "
        f"{strongest['sample_count']} matched sessions"
    )


def _market_pulse_label(snapshot: dict[str, Any], catalyst: dict[str, Any] | None) -> str:
    if catalyst:
        label = _pulse_label(catalyst)
        return f"Risk · {label}" if catalyst.get("sentiment") == "risk" else label
    parts: list[str] = []
    recent_rvol = snapshot.get("recent_relative_volume") or snapshot.get("relative_volume")
    if recent_rvol is not None:
        parts.append(f"{float(recent_rvol):.1f}× volume")
    stage = str(snapshot.get("stage") or "").strip().title()
    if stage:
        parts.append(stage)
    return " · ".join(parts) or "Market move · no recent filing"


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("payload_json")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_timestamp(row: dict[str, Any]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(row.get("event_at") or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _external_event_context(rows: list[dict[str, Any]]) -> dict[str, Any]:
    checked_at = now()
    news: list[dict[str, Any]] = []
    social_by_source: dict[str, dict[str, Any]] = {}
    active_halt: dict[str, Any] | None = None
    for row in rows:
        event = {**row, "payload": _event_payload(row)}
        timestamp = _event_timestamp(event)
        if event.get("event_type") == "news_article":
            if timestamp and timestamp >= checked_at - timedelta(hours=24):
                news.append(event)
        elif event.get("event_type") == "social_spike":
            if timestamp and timestamp >= checked_at - timedelta(hours=6):
                source = str(event.get("source") or "unknown")
                current = social_by_source.get(source)
                if current is None or str(event.get("event_at") or "") > str(
                    current.get("event_at") or ""
                ):
                    social_by_source[source] = event
        elif event.get("event_type") == "trading_halt":
            status = str(event.get("status") or "").lower()
            resume_at = _event_timestamp({"event_at": event["payload"].get("trade_resume_at")})
            if status in {"active", "halted", "pending"} or (
                resume_at is not None and resume_at > checked_at
            ):
                active_halt = active_halt or event

    news.sort(key=lambda event: str(event.get("event_at") or ""), reverse=True)
    social = list(social_by_source.values())
    social.sort(key=lambda event: str(event.get("event_at") or ""), reverse=True)
    mention_count = sum(int(event["payload"].get("mention_count") or 0) for event in social)
    engagement_count = sum(int(event["payload"].get("engagement_count") or 0) for event in social)
    news_boost = min(6.0, 1.5 * math.sqrt(len(news)))
    social_boost = min(
        8.0,
        math.log2(mention_count + 1) + 0.5 * math.log2(engagement_count + 1),
    )
    return {
        "news_count": len(news),
        "news_boost": round(news_boost, 2),
        "latest_news": news[0] if news else None,
        "social_mentions": mention_count,
        "social_engagement": engagement_count,
        "social_search_boost": round(social_boost, 2),
        "latest_social": social[0] if social else None,
        "active_halt": active_halt,
        "safety_penalty": 25.0 if active_halt else 0.0,
        "normalized_event_count": len(rows),
    }


def _external_event_label(context: dict[str, Any]) -> tuple[str, str, str | None] | None:
    halt = context.get("active_halt")
    if halt:
        return (
            f"Trading halt · {halt.get('status') or 'active'}",
            str(halt.get("source") or "nasdaq_trader"),
            halt.get("source_url"),
        )
    latest_news = context.get("latest_news")
    latest_social = context.get("latest_social")
    candidates = [event for event in (latest_news, latest_social) if event]
    if not candidates:
        return None
    latest = max(candidates, key=lambda event: str(event.get("event_at") or ""))
    if latest.get("event_type") == "news_article":
        title = str(latest.get("payload", {}).get("title") or "New company coverage")
        return (
            f"News · {title[:96]}",
            str(latest.get("source") or "news"),
            latest.get("source_url"),
        )
    mentions = int(context.get("social_mentions") or 0)
    network = str(latest.get("payload", {}).get("network_label") or "Social")
    noun = "cashtag mention" if network == "Bluesky" else "mention"
    return (
        f"{network} · {mentions} {noun}{'s' if mentions != 1 else ''}",
        str(latest.get("source") or "social"),
        latest.get("source_url"),
    )


def _pulse_profile_states(
    profile: str | None,
    entries: dict[str, dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not profile or not entries:
        return {}
    tickers = list(entries)
    placeholders = ",".join("?" for _ in tickers)
    with connection() as db:
        rows = db.execute(
            f"""
            SELECT * FROM pulse_profile_state
            WHERE profile_id=? AND ticker IN ({placeholders})
            """,
            (profile, *tickers),
        ).fetchall()
    return {(str(row["ticker"]), str(row["entered_at"])): dict(row) for row in rows}


def _decorate_pulse_attention(
    rows: list[dict[str, Any]],
    profile: str | None,
) -> None:
    entries = {
        str(row["ticker"]): {"time": row["entered_at"]}
        for row in rows
        if row.get("entered_at")
    }
    states = _pulse_profile_states(profile, entries)
    for row in rows:
        ticker = str(row["ticker"])
        entered_at = str(row["entered_at"]) if row.get("entered_at") else None
        state = states.get((ticker, entered_at), {}) if entered_at else {}
        row["first_seen_at"] = state.get("first_seen_at")
        row["inspected_at"] = state.get("inspected_at")
        row["notified_at"] = state.get("notified_at")
        row["novelty_state"] = (
            "normal"
            if not profile or not entered_at
            else "inspected"
            if state.get("inspected_at")
            else "seen"
            if state.get("first_seen_at")
            else "unseen"
        )


def _attach_pulse_entries(rows: list[dict[str, Any]]) -> None:
    entries = _pulse_entry_markers([str(row["ticker"]) for row in rows])
    for row in rows:
        marker = entries.get(str(row["ticker"]))
        row["entered_at"] = str(marker["time"]) if marker else None


def _write_pulse_attention(
    profile: str,
    entries: list[PulseAttentionItem],
    action: str,
) -> int:
    requested = [(item.ticker.strip().upper(), item.entered_at) for item in entries]
    current = _pulse_entry_markers([ticker for ticker, _ in requested])
    valid = [
        (ticker, entered_at)
        for ticker, entered_at in requested
        if TICKER_RE.fullmatch(ticker)
        and current.get(ticker)
        and str(current[ticker]["time"]) == entered_at
    ]
    if not valid:
        return 0
    timestamp = iso()
    with connection() as db:
        if action == "seen":
            db.executemany(
                """
                INSERT INTO pulse_profile_state(
                    profile_id,ticker,entered_at,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(profile_id,ticker,entered_at) DO UPDATE SET
                    first_seen_at=COALESCE(
                        pulse_profile_state.first_seen_at,excluded.first_seen_at
                    ),
                    last_seen_at=excluded.last_seen_at
                """,
                [
                    (profile, ticker, entered_at, timestamp, timestamp)
                    for ticker, entered_at in valid
                ],
            )
        elif action == "inspected":
            db.executemany(
                """
                INSERT INTO pulse_profile_state(
                    profile_id,ticker,entered_at,first_seen_at,last_seen_at,inspected_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(profile_id,ticker,entered_at) DO UPDATE SET
                    first_seen_at=COALESCE(
                        pulse_profile_state.first_seen_at,excluded.first_seen_at
                    ),
                    last_seen_at=excluded.last_seen_at,
                    inspected_at=excluded.inspected_at
                """,
                [
                    (profile, ticker, entered_at, timestamp, timestamp, timestamp)
                    for ticker, entered_at in valid
                ],
            )
        elif action == "notified":
            db.executemany(
                """
                INSERT INTO pulse_profile_state(
                    profile_id,ticker,entered_at,notified_at
                ) VALUES(?,?,?,?)
                ON CONFLICT(profile_id,ticker,entered_at) DO UPDATE SET
                    notified_at=excluded.notified_at
                """,
                [(profile, ticker, entered_at, timestamp) for ticker, entered_at in valid],
            )
        else:
            raise ValueError(f"Unknown Pulse attention action: {action}")
    return len(valid)


def _pulse_data_uncached() -> dict[str, Any]:
    event_cutoff = iso(now() - timedelta(days=3))
    scan_cutoff = iso(now() - timedelta(days=7))
    with connection() as db:
        latest_run = db.execute(
            """
            SELECT id,captured_at FROM scan_runs
            WHERE captured_at>? AND candidate_rows>0
            ORDER BY captured_at DESC LIMIT 1
            """,
            (scan_cutoff,),
        ).fetchone()
        market_rows = (
            db.execute(
                """
                SELECT s.*,
                       (SELECT c.name FROM sec_companies c
                        WHERE c.ticker=s.ticker LIMIT 1) AS listed_company
                FROM scan_snapshots s
                WHERE s.scan_run_id=?
                ORDER BY s.baseline_rank,s.ticker
                """,
                (latest_run["id"],),
            ).fetchall()
            if latest_run
            else []
        )
        prediction_rows = (
            db.execute(
                """
                SELECT p.* FROM ranker_predictions p
                JOIN scan_snapshots s ON s.id=p.snapshot_id
                WHERE s.scan_run_id=? ORDER BY p.created_at DESC
                """,
                (latest_run["id"],),
            ).fetchall()
            if latest_run
            else []
        )
        heart_rows = db.execute(
            """
            SELECT ticker,COUNT(*) AS heart_count FROM ticker_hearts
            WHERE active=1 GROUP BY ticker
            """
        ).fetchall()
        market_event_rows = db.execute(
            """
            SELECT source,ticker,event_type,status,event_at,source_url,payload_json
            FROM market_events
            WHERE event_at>? ORDER BY event_at DESC,last_collected_at DESC
            """,
            (event_cutoff,),
        ).fetchall()
        filing_rows = db.execute(
            """
            SELECT f.*,o.return_1h_pct,o.return_1d_pct,o.return_5d_pct
            FROM sec_filings f
            LEFT JOIN sec_outcomes o ON o.accession=f.accession
            WHERE f.created_at>?
            ORDER BY f.score DESC,f.filed_at DESC
            """,
            (event_cutoff,),
        ).fetchall()

    filings_by_ticker: dict[str, dict[str, Any]] = {}
    filing_counts: dict[str, int] = {}
    for raw in filing_rows:
        event = _intelligence_evidence(dict(raw))
        ticker = event["ticker"]
        filing_counts[ticker] = filing_counts.get(ticker, 0) + 1
        filings_by_ticker.setdefault(ticker, event)

    predictions: dict[str, dict[str, Any]] = {}
    for raw in prediction_rows:
        predictions.setdefault(str(raw["snapshot_id"]), dict(raw))
    hearts = {str(row["ticker"]): int(row["heart_count"]) for row in heart_rows}
    market_events_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for raw in market_event_rows:
        market_events_by_ticker.setdefault(str(raw["ticker"]), []).append(dict(raw))
    active_kol_calls = calls_for_tickers([str(row["ticker"]) for row in market_rows])

    runner_rows: list[dict[str, Any]] = []
    unexplained = 0
    for raw in market_rows:
        snapshot = dict(raw)
        ticker = snapshot["ticker"]
        catalyst = filings_by_ticker.get(ticker)
        if not catalyst:
            unexplained += 1
        prediction = predictions.get(str(snapshot["id"]))
        custom_score = (
            float(prediction["score"])
            if prediction and prediction.get("score") is not None
            else float(snapshot.get("score") or 0)
        )
        catalyst_score = float(catalyst.get("score") or 0) if catalyst else 0.0
        catalyst_sentiment = str(catalyst.get("sentiment") or "") if catalyst else ""
        event_boost = (
            min(12.0, catalyst_score * 0.12)
            if catalyst_sentiment == "positive"
            else -min(25.0, catalyst_score * 0.25)
            if catalyst_sentiment == "risk"
            else 0.0
        )
        heart_count = hearts.get(ticker, 0)
        community_boost = min(8.0, math.log2(heart_count + 1) * 2.0)
        external = _external_event_context(market_events_by_ticker.get(ticker, []))
        news_boost = float(external["news_boost"])
        social_search_boost = float(external["social_search_boost"])
        safety_penalty = float(external["safety_penalty"])
        raw_rug_score = snapshot.get("rug_score")
        rug_score = float(raw_rug_score) if raw_rug_score is not None else None
        trade_state = str(snapshot.get("trade_state") or "UNKNOWN").upper()
        if external.get("active_halt"):
            rug_score = max(rug_score or 0.0, 90.0)
            trade_state = "AVOID"
        rug_penalty = (rug_score or 0.0) * 0.30
        state_penalty = 25.0 if trade_state == "EXIT" else 20.0 if trade_state == "AVOID" else 0.0
        pulse_score = round(
            max(
                0.0,
                min(
                    100.0,
                    custom_score
                    + event_boost
                    + news_boost
                    + social_search_boost
                    + community_boost
                    - safety_penalty
                    - rug_penalty
                    - state_penalty,
                ),
            ),
            2,
        )
        score_components = {
            "market": round(custom_score, 2),
            "sec_event": round(event_boost, 2),
            "news": news_boost,
            "social_search": social_search_boost,
            "community": round(community_boost, 2),
            "safety": -safety_penalty,
        }
        if snapshot.get("rug_score") is not None or snapshot.get("trade_state") is not None:
            score_components.update({"rug": -round(rug_penalty, 2), "state": -state_penalty})
        external_label = _external_event_label(external)
        runner = {
            **snapshot,
            "baseline_score": float(snapshot.get("score") or 0),
            "setup_score": float(snapshot.get("setup_score") or snapshot.get("score") or 0),
            "rug_score": rug_score,
            "trade_state": trade_state,
            "model_score": custom_score if prediction else None,
            "model_rank": prediction.get("rank") if prediction else None,
            "score": pulse_score,
            "custom_score": pulse_score,
            "runner_probability": prediction.get("probability_up") if prediction else None,
            "expected_return_pct": (prediction.get("expected_return_pct") if prediction else None),
            "heart_count": heart_count,
            "event_boost": round(event_boost, 2),
            "news_boost": news_boost,
            "social_search_boost": social_search_boost,
            "community_boost": round(community_boost, 2),
            "safety_penalty": safety_penalty,
            "rug_penalty": round(rug_penalty, 2),
            "state_penalty": state_penalty,
            "active_market_event": external.get("active_halt"),
            "news_count": external["news_count"],
            "external_social_mentions": external["social_mentions"],
            "external_social_engagement": external["social_engagement"],
            "latest_news": external.get("latest_news"),
            "score_components": score_components,
            "company": (
                catalyst.get("company", ticker)
                if catalyst
                else snapshot.get("listed_company") or ticker
            ),
            "kind": catalyst.get("kind") if catalyst else "No recent SEC catalyst",
            "sentiment": catalyst.get("sentiment") if catalyst else "gap",
            "form": catalyst.get("form", "") if catalyst else "",
            "coin_label": ticker[:2],
            "coin_tone": _coin_tone(ticker),
            "pulse_label": (
                _market_pulse_label(snapshot, catalyst)
                if catalyst
                else external_label[0]
                if external_label
                else _market_pulse_label(snapshot, None)
            ),
            "event_count": (filing_counts.get(ticker, 0) + int(external["normalized_event_count"])),
            "source": "market",
            "section": "scored",
            "event_at": snapshot["captured_at"],
            "attention_score": pulse_score,
            "filing_url": (
                catalyst.get("filing_url")
                if catalyst
                else external_label[2]
                if external_label
                else None
            ),
            "signals": _json_list(snapshot.get("signals_json")),
            "risks": _json_list(snapshot.get("risks_json")),
            "kol_calls": active_kol_calls.get(str(ticker), []),
        }
        runner["evidence_gate"] = _evidence_gate(
            runner,
            [catalyst] if catalyst else [],
            external_context=external,
        )
        runner_rows.append(runner)

    runner_rows.sort(
        key=lambda row: (
            -float(row["custom_score"]),
            int(row.get("baseline_rank") or 1_000_000),
            str(row["ticker"]),
        )
    )
    for custom_rank, runner in enumerate(runner_rows, start=1):
        runner["custom_rank"] = custom_rank
    _attach_pulse_entries(runner_rows)
    market_updated_at = str(latest_run["captured_at"]) if latest_run else None
    return {
        "rows": runner_rows,
        "stats": {
            "live": len(runner_rows),
            "runners": len(runner_rows),
            "unexplained": unexplained,
            "filings": sum(filing_counts.get(row["ticker"], 0) for row in runner_rows),
        },
        "updated_at": market_updated_at,
        "market_updated_at": market_updated_at,
        "kols": predictor_scorecards(),
        "next_offset": len(runner_rows),
        "has_more": False,
    }


def _refresh_pulse_base(cache_key: str) -> None:
    try:
        payload = _pulse_data_uncached()
        with PULSE_DATA_LOCK:
            PULSE_DATA_CACHE[cache_key] = (time.monotonic(), payload)
    except Exception:
        LOG.exception("Pulse cache refresh failed")
    finally:
        with PULSE_DATA_LOCK:
            PULSE_DATA_REFRESHING.discard(cache_key)


def _pulse_base_data() -> dict[str, Any]:
    cache_key = str(runner_db.DATABASE_PATH)
    with PULSE_DATA_LOCK:
        current = time.monotonic()
        cached = PULSE_DATA_CACHE.get(cache_key)
        if cached and current - cached[0] < PULSE_CACHE_TTL_SECONDS:
            return cached[1]
        if cached:
            if cache_key not in PULSE_DATA_REFRESHING:
                PULSE_DATA_REFRESHING.add(cache_key)
                threading.Thread(
                    target=_refresh_pulse_base,
                    args=(cache_key,),
                    daemon=True,
                    name="pulse-cache-refresh",
                ).start()
            return cached[1]
        payload = _pulse_data_uncached()
        if cache_key not in PULSE_DATA_CACHE and len(PULSE_DATA_CACHE) >= 8:
            PULSE_DATA_CACHE.clear()
        PULSE_DATA_CACHE[cache_key] = (time.monotonic(), payload)
        return payload


def pulse_data(
    *,
    offset: int = 0,
    limit: int = 50,
    profile: str | None = None,
) -> dict[str, Any]:
    offset = max(0, offset)
    limit = max(1, min(limit, 50))
    base = _pulse_base_data()
    total = len(base["rows"])
    rows = [dict(row) for row in base["rows"][offset : offset + limit]]
    _decorate_pulse_attention(rows, profile)
    return {
        **base,
        "rows": rows,
        "stats": {**base["stats"], "live": len(rows)},
        "next_offset": offset + len(rows),
        "has_more": offset + len(rows) < total,
    }


def _report_record(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    report = dict(row)
    for key in ("catalysts_json", "risks_json", "watch_json", "sources_json"):
        report[key.removesuffix("_json")] = _json_list(report.get(key))
    return report


def _commission_record(
    row: Any,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not row:
        return None
    report = dict(row)
    for key in ("catalysts_json", "risks_json", "watch_json", "unknowns_json"):
        report[key.removesuffix("_json")] = _json_list(report.get(key))
    report["company_profile"] = _json_container(report.get("company_profile_json"), {})
    report["people"] = _json_container(report.get("people_json"), [])
    report["filing_context"] = _json_container(report.get("filing_context_json"), [])
    report["sources"] = [
        {"url": source, "label": _source_label(source)}
        for source in _json_list(report.get("sources_json"))
        if _safe_source_url(source)
    ]
    try:
        report["usage"] = json.loads(report.get("usage_json") or "{}")
    except (TypeError, ValueError):
        report["usage"] = {}
    report["actor"] = _json_container(report.get("actor_snapshot_json"), {})
    if not report["actor"] and report.get("actor_id") == FLASH.id:
        report["actor"] = actor_snapshot()
    summary = summary or _ticker_summary(report["ticker"])
    report["company"] = summary["company"] if summary else report["ticker"]
    report["coin_label"] = summary["coin_label"] if summary else report["ticker"][:2]
    report["coin_tone"] = summary["coin_tone"] if summary else _coin_tone(report["ticker"])
    return report


def commissioned_reports(limit: int = 12) -> list[dict[str, Any]]:
    with connection() as db:
        rows = db.execute(
            """
            SELECT c.* FROM research_commissions c
            WHERE c.status='complete'
            ORDER BY c.completed_at DESC LIMIT ?
            """,
            (max(1, min(limit, 50)),),
        ).fetchall()
    return [report for row in rows if (report := _commission_record(row))]


def _alpha_list_summary(
    ticker: str,
    pulse_lookup: dict[str, dict[str, Any]],
    fallback_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if ticker in pulse_lookup:
        return dict(pulse_lookup[ticker])
    item = dict(fallback_lookup.get(ticker, {}))
    item.setdefault("ticker", ticker)
    item.setdefault("company", ticker)
    item.setdefault("coin_label", ticker[:2])
    item.setdefault("coin_tone", _coin_tone(ticker))
    if item.get("captured_at"):
        item["source"] = "market"
        item["event_at"] = item["captured_at"]
        item["sentiment"] = item.get("catalyst_sentiment") or "gap"
        item["pulse_label"] = _market_pulse_label(item, None)
    elif item.get("filed_at"):
        item = _intelligence_evidence(item)
        item["source"] = "sec"
        item["event_at"] = item["filed_at"]
        item["pulse_label"] = _pulse_label(item)
    else:
        item.update(
            source="quiet",
            event_at=None,
            sentiment="neutral",
            pulse_label="Quiet",
        )
    return item


def _alpha_base_data_uncached() -> dict[str, Any]:
    with connection() as db:
        heart_rows = db.execute(
            """
            SELECT ticker,COUNT(*) AS heart_count,MAX(updated_at) AS latest_heart
            FROM ticker_hearts WHERE active=1
            GROUP BY ticker ORDER BY heart_count DESC,latest_heart DESC LIMIT 50
            """
        ).fetchall()
        report_rows = db.execute(
            """
            SELECT * FROM alpha_reports WHERE status='complete'
            ORDER BY ticker,created_at DESC
            """
        ).fetchall()
        commission_rows = db.execute(
            """
            SELECT c.* FROM research_commissions c
            WHERE c.status='complete'
            ORDER BY c.completed_at DESC LIMIT 12
            """
        ).fetchall()

    pulse = pulse_data(limit=50)
    pulse_lookup = {str(row["ticker"]): row for row in pulse["rows"]}
    requested = [str(row["ticker"]) for row in heart_rows]
    requested.extend(str(row["ticker"]) for row in commission_rows)
    fallback_lookup = _radar_market_summaries(
        [ticker for ticker in requested if ticker not in pulse_lookup]
    )
    summary_lookup = {
        ticker: _alpha_list_summary(ticker, pulse_lookup, fallback_lookup)
        for ticker in dict.fromkeys(requested)
    }
    latest_reports: dict[str, Any] = {}
    for report_row in report_rows:
        latest_reports.setdefault(str(report_row["ticker"]), report_row)

    rows: list[dict[str, Any]] = []
    for rank, heart_row in enumerate(heart_rows, start=1):
        ticker = str(heart_row["ticker"])
        item = dict(summary_lookup[ticker])
        item.update(
            rank=rank,
            heart_count=int(heart_row["heart_count"]),
            latest_heart=heart_row["latest_heart"],
            is_leader=rank == 1,
            ai_report=_report_record(latest_reports.get(ticker)),
        )
        rows.append(item)
    ranked = {row["ticker"] for row in rows}
    contenders = [row for row in pulse["rows"][:8] if row["ticker"] not in ranked][:5]
    commissions = [
        report
        for raw in commission_rows
        if (
            report := _commission_record(
                raw,
                summary_lookup.get(str(raw["ticker"])),
            )
        )
    ]
    return {
        "rows": rows,
        "contenders": contenders,
        "total_hearts": sum(row["heart_count"] for row in rows),
        "provider_ready": bool(AI_REPORT_API_KEY),
        "commissions": commissions,
    }


def _refresh_alpha_base(cache_key: str) -> None:
    try:
        payload = _alpha_base_data_uncached()
        with ALPHA_DATA_LOCK:
            ALPHA_DATA_CACHE[cache_key] = (
                time.monotonic() + ALPHA_CACHE_TTL_SECONDS,
                payload,
            )
    except Exception:
        LOG.exception("Alpha cache refresh failed")
    finally:
        with ALPHA_DATA_LOCK:
            ALPHA_DATA_REFRESHING.discard(cache_key)


def _alpha_base_data() -> dict[str, Any]:
    cache_key = str(runner_db.DATABASE_PATH)
    current = time.monotonic()
    with ALPHA_DATA_LOCK:
        cached = ALPHA_DATA_CACHE.get(cache_key)
        if cached and current < cached[0]:
            return cached[1]
        if cached:
            if cache_key not in ALPHA_DATA_REFRESHING:
                ALPHA_DATA_REFRESHING.add(cache_key)
                threading.Thread(
                    target=_refresh_alpha_base,
                    args=(cache_key,),
                    daemon=True,
                    name="alpha-cache-refresh",
                ).start()
            return cached[1]
        payload = _alpha_base_data_uncached()
        if len(ALPHA_DATA_CACHE) >= 8 and cache_key not in ALPHA_DATA_CACHE:
            oldest_key = min(ALPHA_DATA_CACHE, key=lambda key: ALPHA_DATA_CACHE[key][0])
            ALPHA_DATA_CACHE.pop(oldest_key, None)
        ALPHA_DATA_CACHE[cache_key] = (current + ALPHA_CACHE_TTL_SECONDS, payload)
        return payload


def alpha_board_data(profile: str) -> dict[str, Any]:
    base = _alpha_base_data()
    with connection() as db:
        mine = {
            row["ticker"]
            for row in db.execute(
                "SELECT ticker FROM ticker_hearts WHERE profile_id=? AND active=1",
                (profile,),
            ).fetchall()
        }
    return {
        **base,
        "rows": [
            {**row, "hearted": row["ticker"] in mine}
            for row in base["rows"]
        ],
    }


def _alpha_evidence(ticker: str, heart_count: int) -> tuple[str, dict[str, Any]]:
    detail = ticker_detail_data(ticker)
    if not detail:
        raise ValueError("Ticker detail is unavailable")
    current = detail["current"]
    filings = [
        {
            "accession": event.get("accession"),
            "form": event.get("form"),
            "filed_at": event.get("filed_at"),
            "kind": event.get("kind"),
            "sentiment": event.get("sentiment"),
            "label": event.get("evidence_label"),
            "text": event.get("evidence_text"),
            "url": event.get("filing_url"),
            "actor": event.get("actor"),
            "actor_title": event.get("actor_title"),
            "beneficial_owner_names": [
                name.strip()
                for name in str(event.get("beneficial_owner_names") or "").split(",")
                if name.strip()
            ],
            "reporting_person_types": [
                kind.strip()
                for kind in str(event.get("reporting_person_types") or "").split(",")
                if kind.strip()
            ],
            "beneficial_ownership_pct": event.get("beneficial_ownership_pct"),
            "beneficial_shares": event.get("beneficial_shares"),
            "transaction_codes": event.get("transaction_codes"),
            "transaction_shares": event.get("transaction_shares"),
            "transaction_price": event.get("transaction_price"),
            "transaction_value": event.get("transaction_value"),
            "post_transaction_shares": event.get("post_transaction_shares"),
            "stake_change_pct": event.get("stake_change_pct"),
            "is_10b5_1": bool(event.get("is_10b5_1")),
            "direct_ownership": event.get("direct_ownership"),
            "footnotes": event.get("footnotes"),
        }
        for event in detail["events"][:5]
    ]
    evidence = {
        "ticker": ticker,
        "company": detail["company"],
        "exchange": detail["exchange"],
        "heart_count": heart_count,
        "captured_at": current.get("event_at"),
        "price": current.get("price"),
        "change_pct": current.get("change_pct"),
        "score": current.get("score"),
        "setup_score": current.get("setup_score") or current.get("score"),
        "rug_score": current.get("rug_score"),
        "rug_level": current.get("rug_level"),
        "trade_state": current.get("trade_state"),
        "state_reason": current.get("state_reason"),
        "hard_veto": bool(current.get("hard_veto")),
        "crash_candidate": bool(current.get("crash_candidate")),
        "drawdown_20d_pct": current.get("drawdown_20d_pct"),
        "drawdown_90d_pct": current.get("drawdown_90d_pct"),
        "drawdown_52w_pct": current.get("drawdown_52w_pct"),
        "rebound_from_20d_low_pct": current.get("rebound_from_20d_low_pct"),
        "issuer_risk": current.get("issuer_risk", {}),
        "stage": current.get("stage"),
        "relative_volume": current.get("relative_volume"),
        "recent_relative_volume": current.get("recent_relative_volume"),
        "momentum_15m_pct": current.get("momentum_15m_pct"),
        "signals": current.get("signals", []),
        "risks": current.get("risks", []),
        "evidence_checks": detail["evidence_gate"]["checks"],
        "filings": filings,
    }
    fingerprint = json.dumps(
        {
            "research_version": "identity-thesis-v1",
            "ticker": ticker,
            "event_at": current.get("event_at"),
            "filings": [
                {
                    "accession": item.get("accession"),
                    "filed_at": item.get("filed_at"),
                    "actor": item.get("actor"),
                    "owners": item.get("beneficial_owner_names"),
                }
                for item in filings
            ],
            "checks": detail["evidence_gate"]["checks"],
            "blockers": detail["evidence_gate"].get("blockers", []),
            "rug_score": current.get("rug_score"),
            "trade_state": current.get("trade_state"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:24], evidence


class ReportGenerationFailure(HTTPException):
    """A user-safe report failure with provider metadata but no report content."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        diagnostics: dict[str, Any],
    ) -> None:
        super().__init__(status_code=status_code, detail=detail)
        self.diagnostics = diagnostics


def _openrouter_diagnostics(
    result: Any,
    *,
    choice: Any = None,
    message: Any = None,
    content: Any = None,
) -> dict[str, Any]:
    """Keep enough response metadata to debug failures without storing model output."""

    payload = result if isinstance(result, dict) else {}
    selected = choice if isinstance(choice, dict) else {}
    reply = message if isinstance(message, dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    if isinstance(content, str):
        content_chars = len(content)
        content_type = "text"
    elif isinstance(content, list):
        content_chars = sum(
            len(str(item.get("text") or "")) for item in content if isinstance(item, dict)
        )
        content_type = "parts"
    elif content is None:
        content_chars = 0
        content_type = "missing"
    else:
        content_chars = 0
        content_type = type(content).__name__
    diagnostics: dict[str, Any] = {
        "phase": "provider_response",
        "provider_request_id": str(payload.get("id") or "")[:120] or None,
        "model": str(payload.get("model") or "")[:160] or None,
        "finish_reason": str(selected.get("finish_reason") or "")[:80] or None,
        "native_finish_reason": (str(selected.get("native_finish_reason") or "")[:80] or None),
        "content_type": content_type,
        "content_chars": content_chars,
        "refused": bool(reply.get("refusal")),
    }
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "reasoning_tokens",
    ):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            diagnostics[key] = value
    return diagnostics


def _openrouter_report_json(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    if not isinstance(content, str) or not content.strip():
        raise ValueError("missing content")
    text = content.strip()
    if text.startswith("```"):
        first_break = text.find("\n")
        if first_break >= 0:
            text = text[first_break + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if isinstance(parsed, dict) and isinstance(parsed.get("report"), dict):
        parsed = parsed["report"]
    if not isinstance(parsed, dict):
        raise ValueError("report is not an object")
    return parsed


def _report_text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _report_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            output.append(item.strip())
        elif isinstance(item, dict):
            text = _report_text(item.get("text") or item.get("description") or item.get("label"))
            if text:
                output.append(text)
    return output


def _normalize_openrouter_report(
    raw_report: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Fill optional report fields while preserving the model's usable thesis."""

    normalized_fields: list[str] = []
    headline = _report_text(raw_report.get("headline"))
    thesis = _report_text(raw_report.get("thesis"))
    summary = _report_text(raw_report.get("summary"))
    if not thesis and summary:
        thesis = summary
        normalized_fields.append("thesis")
    if not summary and thesis:
        summary = thesis
        normalized_fields.append("summary")
    if not thesis or not summary:
        raise ValueError("missing usable thesis")
    if not headline:
        headline = thesis.split(".", 1)[0].strip() or f"{evidence.get('ticker', 'Stock')} report"
        normalized_fields.append("headline")

    company_profile = raw_report.get("company_profile")
    if not isinstance(company_profile, dict):
        company_profile = {}
        normalized_fields.append("company_profile")

    aliases = {
        "people": ("people", "relevant_people", "persons"),
        "filings": ("filings", "filing_context"),
    }
    structured: dict[str, list[dict[str, Any]]] = {}
    for field, candidates in aliases.items():
        value = next((raw_report.get(key) for key in candidates if key in raw_report), None)
        if not isinstance(value, list):
            normalized_fields.append(field)
            value = []
        structured[field] = [item for item in value if isinstance(item, dict)]

    text_lists: dict[str, list[str]] = {}
    for field in ("catalysts", "risks", "watch", "unknowns"):
        value = raw_report.get(field)
        if not isinstance(value, list):
            normalized_fields.append(field)
        text_lists[field] = _report_text_list(value)

    source_values = raw_report.get("sources")
    if not isinstance(source_values, list):
        normalized_fields.append("sources")
        source_values = []
    sources = [item for item in source_values if isinstance(item, str)]
    return (
        {
            **raw_report,
            "headline": headline[:180],
            "thesis": thesis,
            "summary": summary,
            "company_profile": company_profile,
            **structured,
            **text_lists,
            "sources": sources,
        },
        list(dict.fromkeys(normalized_fields)),
    )


def _generate_openrouter_report(
    openrouter_key: str,
    evidence: dict[str, Any],
    user_id: str,
    *,
    actor: AIKol = FLASH,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    request_payload = {
        "actor": actor_snapshot(actor),
        "task": (
            "Identify the issuer and each person named in the filings. Explain the filings, "
            "ownership changes, news, and social posts. Then form a thesis from the supplied "
            "business, financing, ownership, market, and media evidence."
        ),
        "output": {
            "headline": "short, direct thesis",
            "thesis": "2-4 short sentences; bullish, bearish, mixed, or watch; say why",
            "summary": "plain English; no metric dump or filler",
            "company_profile": {
                "what_it_does": "products, customers, and business model",
                "stage": "operating or clinical stage and main assets",
                "why_it_matters": "the company fact most relevant to this setup",
                "source_urls": [],
            },
            "people": [
                {
                    "name": "person or entity",
                    "role": "current verified role",
                    "filing_role": "why named in the filing",
                    "relevance": "why this person matters to the thesis",
                    "action": "purchase, sale, ownership disclosure, or other action",
                    "confidence": "verified, partial, or unknown",
                    "source_urls": [],
                }
            ],
            "filings": [
                {
                    "form": "SEC form",
                    "filed_at": "date",
                    "plain_english": "what happened",
                    "why_it_matters": "effect on the thesis or rug risk",
                    "source_url": "provided SEC URL",
                }
            ],
            "catalysts": [],
            "risks": [],
            "watch": [],
            "unknowns": [],
            "sources": [],
        },
        "evidence": evidence,
    }
    body = {
        "model": actor.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    f"You are {actor.display_name}, Runner Watch's degen research voice. "
                    "Use short, simple English. Slang only when precise. No hype or filler. "
                    "Use supplied evidence only. Mark unknowns. Return JSON."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(request_payload, separators=(",", ":")),
            },
        ],
        "response_format": {"type": "json_object"},
        "provider": {"require_parameters": True},
        "reasoning_effort": "high",
        "max_tokens": OPENROUTER_RESEARCH_OUTPUT_TOKENS,
        "user": hashlib.sha256(user_id.encode()).hexdigest()[:32],
    }
    api_request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {openrouter_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": APP_ORIGIN,
            "X-OpenRouter-Title": "Runner Watch",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(api_request, timeout=110) as response:  # noqa: S310
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            message = "Reconnect OpenRouter and try again."
        elif exc.code == 402:
            message = "OpenRouter needs credits before it can run this report."
        elif exc.code == 429:
            message = "OpenRouter is busy. Try again in a moment."
        else:
            message = "OpenRouter could not complete this report."
        raise ReportGenerationFailure(
            exc.code if exc.code < 500 else 502,
            message,
            {"phase": "provider_http", "http_status": exc.code},
        ) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise ReportGenerationFailure(
            504,
            "Flash took too long to answer. Retry Flash.",
            {"phase": "provider_timeout"},
        ) from exc
    except (TypeError, ValueError) as exc:
        raise ReportGenerationFailure(
            502,
            "OpenRouter returned an unreadable response. Retry Flash.",
            {"phase": "provider_envelope", "failure_kind": "invalid_json"},
        ) from exc
    choice: Any = None
    message: Any = None
    content: Any = None
    try:
        choice = result["choices"][0]
        message = choice["message"]
        content = message.get("content")
        raw_report = _openrouter_report_json(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        diagnostics = _openrouter_diagnostics(
            result,
            choice=choice,
            message=message,
            content=content,
        )
        diagnostics["failure_kind"] = "invalid_json"
        detail = (
            "Flash ran out of room before finishing the report. Retry Flash."
            if diagnostics.get("finish_reason") == "length"
            else "Flash returned a malformed report. Retry Flash."
        )
        raise ReportGenerationFailure(502, detail, diagnostics) from exc
    try:
        report, normalized_fields = _normalize_openrouter_report(raw_report, evidence)
    except ValueError as exc:
        diagnostics = _openrouter_diagnostics(
            result,
            choice=choice,
            message=message,
            content=content,
        )
        diagnostics["failure_kind"] = "missing_core_narrative"
        diagnostics["present_fields"] = sorted(str(key)[:80] for key in raw_report)[:30]
        diagnostics["missing_fields"] = sorted(
            field
            for field in (
                "headline",
                "thesis",
                "summary",
                "company_profile",
                "people",
                "filings",
                "catalysts",
                "risks",
                "watch",
                "unknowns",
                "sources",
            )
            if field not in raw_report
        )
        raise ReportGenerationFailure(
            502,
            "Flash returned a report without a usable thesis. Retry Flash.",
            diagnostics,
        ) from exc
    approved_sources = [
        source for value in evidence.get("sources", []) if (source := _safe_source_url(value))
    ][:100]
    approved_set = set(approved_sources)
    company_sources = report["company_profile"].get("source_urls") or []
    report["company_profile"]["source_urls"] = [
        source
        for value in company_sources
        if (source := _safe_source_url(value)) and source in approved_set
    ][:4]
    clean_people = []
    for person in report["people"][:12]:
        if not isinstance(person, dict) or not str(person.get("name") or "").strip():
            continue
        person["source_urls"] = [
            source
            for value in person.get("source_urls") or []
            if (source := _safe_source_url(value)) and source in approved_set
        ][:4]
        clean_people.append(person)
    report["people"] = clean_people
    clean_filings = []
    for filing in report["filings"][:8]:
        if not isinstance(filing, dict):
            continue
        source = _safe_source_url(filing.get("source_url"))
        filing["source_url"] = source if source in approved_set else None
        clean_filings.append(filing)
    report["filings"] = clean_filings
    report["sources"] = approved_sources
    usage = dict(result.get("usage") or {})
    usage["generation"] = {
        **_openrouter_diagnostics(
            result,
            choice=choice,
            message=message,
            content=content,
        ),
        "normalized_fields": normalized_fields,
    }
    return report, str(result.get("model") or actor.model), usage


def _fallback_people_from_evidence(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    seen: set[str] = set()
    for filing in evidence.get("filings", []):
        source = _safe_source_url(filing.get("url"))
        names = []
        if filing.get("actor"):
            names.append((str(filing["actor"]), str(filing.get("actor_title") or "Insider")))
        names.extend(
            (str(name), "Beneficial owner") for name in filing.get("beneficial_owner_names", [])
        )
        for name, role in names:
            identity = name.casefold()
            if identity in seen:
                continue
            seen.add(identity)
            people.append(
                {
                    "name": name,
                    "role": role,
                    "filing_role": f"Named in Form {filing.get('form') or 'SEC filing'}",
                    "relevance": "Named by the filing; more background was not verified.",
                    "action": filing.get("text") or filing.get("kind") or "Ownership report",
                    "confidence": "partial",
                    "source_urls": [source] if source else [],
                }
            )
    return people[:12]


def _fallback_filings_from_evidence(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "form": filing.get("form"),
            "filed_at": filing.get("filed_at"),
            "plain_english": filing.get("text") or filing.get("label") or "SEC filing",
            "why_it_matters": filing.get("kind") or "Needs review",
            "source_url": _safe_source_url(filing.get("url")),
        }
        for filing in evidence.get("filings", [])[:8]
    ]


def _create_research_commission(
    user_id: str,
    ticker: str,
    *,
    actor: AIKol = FLASH,
) -> tuple[dict[str, Any], bool]:
    """Create one idempotent server job without storing the provider key."""

    timestamp = iso()
    with connection() as db:
        running = db.execute(
            """
            SELECT * FROM research_commissions
            WHERE user_id=? AND ticker=? AND actor_id=? AND status='running'
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, ticker, actor.id),
        ).fetchone()
        if running:
            return _commission_record(running) or {}, False
        heart_count = db.execute(
            "SELECT COUNT(*) FROM ticker_hearts WHERE ticker=? AND active=1", (ticker,)
        ).fetchone()[0]

    evidence_key, _ = _alpha_evidence(ticker, int(heart_count))
    with connection() as db:
        current = db.execute(
            """
            SELECT * FROM research_commissions
            WHERE user_id=? AND ticker=? AND actor_id=? AND evidence_key=?
                AND status='complete'
            ORDER BY completed_at DESC LIMIT 1
            """,
            (user_id, ticker, actor.id, evidence_key),
        ).fetchone()
        if current:
            return _commission_record(current) or {}, False
        recent_count = db.execute(
            """
            SELECT COUNT(*) FROM research_commissions
            WHERE user_id=? AND status IN ('running','complete') AND created_at>?
            """,
            (user_id, iso(now() - timedelta(days=1))),
        ).fetchone()[0]
        if recent_count >= 3:
            raise HTTPException(429, "You can commission three reports per day.")

    report_id = str(uuid.uuid4())
    public_id = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]
    with connection() as db:
        try:
            db.execute(
                """
                INSERT INTO research_commissions(
                    id,public_id,user_id,ticker,evidence_key,status,requested_model,
                    actor_id,actor_snapshot_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,'running',?,?,?,?,?)
                """,
                (
                    report_id,
                    public_id,
                    user_id,
                    ticker,
                    evidence_key,
                    actor.model,
                    actor.id,
                    json.dumps(actor_snapshot(actor), separators=(",", ":")),
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            running = db.execute(
                """
                SELECT * FROM research_commissions
                WHERE user_id=? AND ticker=? AND actor_id=? AND status='running'
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, ticker, actor.id),
            ).fetchone()
            if running:
                return _commission_record(running) or {}, False
            raise HTTPException(409, "This report is already running.") from exc
        row = db.execute(
            "SELECT * FROM research_commissions WHERE id=?", (report_id,)
        ).fetchone()
    return _commission_record(row) or {}, True


def _run_research_commission(
    report_id: str,
    openrouter_key: str,
    *,
    actor: AIKol = FLASH,
) -> dict[str, Any]:
    with connection() as db:
        row = db.execute(
            "SELECT * FROM research_commissions WHERE id=?", (report_id,)
        ).fetchone()
        if not row:
            raise RuntimeError("Research job not found")
        commission = _commission_record(row) or {}
        if commission.get("status") != "running":
            return commission
        ticker = str(row["ticker"])
        user_id = str(row["user_id"])
        heart_count = db.execute(
            "SELECT COUNT(*) FROM ticker_hearts WHERE ticker=? AND active=1", (ticker,)
        ).fetchone()[0]

    _, evidence = _alpha_evidence(ticker, int(heart_count))
    try:
        research_context = build_research_context(ticker, evidence, model=actor.model)
        report, model, usage = _generate_openrouter_report(
            openrouter_key, research_context, user_id, actor=actor
        )
        thesis = str(report.get("thesis") or report.get("summary") or "")
        company_profile = report.get("company_profile")
        if not isinstance(company_profile, dict):
            company_profile = {
                "what_it_does": "The stored context did not verify the business description.",
                "stage": "unknown",
                "why_it_matters": f"The SEC company map identifies {evidence['company']}.",
                "source_urls": [],
            }
        people = report.get("people")
        if not isinstance(people, list) or not people:
            people = _fallback_people_from_evidence(evidence)
        filing_context = report.get("filings")
        if not isinstance(filing_context, list) or not filing_context:
            filing_context = _fallback_filings_from_evidence(evidence)
        unknowns = report.get("unknowns")
        if not isinstance(unknowns, list):
            unknowns = []
        sources = report.get("sources")
        if not isinstance(sources, list) or not sources:
            sources = research_context.get("sources", [])
        usage = {
            **usage,
            "research_mode": "one_shot_system_context",
            "context": research_context.get("context_stats", {}),
        }
        completed_at = iso()
        with connection() as db:
            db.execute(
                """
                UPDATE research_commissions SET status='complete',model=?,headline=?,summary=?,
                    thesis=?,company_profile_json=?,people_json=?,filing_context_json=?,
                    catalysts_json=?,risks_json=?,watch_json=?,unknowns_json=?,sources_json=?,
                    usage_json=?,research_mode='one_shot_system_context',error=NULL,
                    updated_at=?,completed_at=? WHERE id=?
                """,
                (
                    model[:160],
                    str(report["headline"])[:180],
                    str(report["summary"])[:1800],
                    thesis[:2400],
                    json.dumps(company_profile),
                    json.dumps(people[:12]),
                    json.dumps(filing_context[:8]),
                    json.dumps(list(report["catalysts"])[:8]),
                    json.dumps(list(report["risks"])[:8]),
                    json.dumps(list(report["watch"])[:8]),
                    json.dumps(unknowns[:8]),
                    json.dumps(sources[:20]),
                    json.dumps(usage),
                    completed_at,
                    completed_at,
                    report_id,
                ),
            )
        with connection() as db:
            row = db.execute(
                "SELECT * FROM research_commissions WHERE id=?", (report_id,)
            ).fetchone()
        with ALPHA_DATA_LOCK:
            ALPHA_DATA_CACHE.clear()
        return _commission_record(row) or {}
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else "Report generation failed."
        diagnostics = getattr(exc, "diagnostics", None)
        failure_usage = (
            json.dumps({"failure": diagnostics}, separators=(",", ":"))
            if isinstance(diagnostics, dict)
            else "{}"
        )
        with connection() as db:
            db.execute(
                """
                UPDATE research_commissions
                SET status='failed',error=?,usage_json=?,updated_at=? WHERE id=?
                """,
                (str(detail)[:500], failure_usage, iso(), report_id),
            )
        raise


def _commission_research(
    user_id: str,
    ticker: str,
    openrouter_key: str,
    *,
    actor: AIKol = FLASH,
) -> dict[str, Any]:
    """Run a commission inline for internal callers and focused tests."""

    commission, created = _create_research_commission(user_id, ticker, actor=actor)
    if not created:
        return commission
    return _run_research_commission(commission["id"], openrouter_key, actor=actor)


def _fail_orphaned_research_jobs() -> None:
    """Release jobs whose in-memory provider key disappeared on a server restart."""

    timestamp = iso()
    with connection() as db:
        db.execute(
            """
            UPDATE research_commissions
            SET status='failed',error=?,updated_at=?
            WHERE status='running'
            """,
            ("The server restarted before Flash finished. Please retry.", timestamp),
        )


async def research_job_worker() -> None:
    """Finish commissioned reports independently of the browser request."""

    while True:
        report_id, openrouter_key = await RESEARCH_JOB_QUEUE.get()
        try:
            await run_in_threadpool(_run_research_commission, report_id, openrouter_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception("Flash research job failed: %s", report_id)
        finally:
            openrouter_key = ""
            RESEARCH_JOB_QUEUE.task_done()


def get_commission(public_id: str) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            """
            SELECT * FROM research_commissions
            WHERE public_id=? AND status='complete'
            """,
            (public_id,),
        ).fetchone()
    return _commission_record(row)


def latest_commission(user_id: str, ticker: str) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            """
            SELECT * FROM research_commissions
            WHERE user_id=? AND ticker=? AND actor_id=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, ticker, FLASH.id),
        ).fetchone()
    return _commission_record(row)


def _commission_api_payload(report: dict[str, Any]) -> dict[str, Any]:
    status = str(report.get("status") or "failed")
    public_id = str(report.get("public_id") or "")
    return {
        "ok": status != "failed",
        "ticker": str(report.get("ticker") or ""),
        "job_id": public_id,
        "status": status,
        "retryable": status == "failed",
        "url": f"/research/{public_id}" if status == "complete" and public_id else None,
        "error": str(report.get("error") or "") if status == "failed" else None,
    }


def _generate_alpha_report(evidence: dict[str, Any]) -> dict[str, Any]:
    if not AI_REPORT_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    schema = {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "summary": {"type": "string"},
            "catalysts": {"type": "array", "items": {"type": "string"}},
            "risks": {"type": "array", "items": {"type": "string"}},
            "watch": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["headline", "summary", "catalysts", "risks", "watch"],
        "additionalProperties": False,
    }
    body = {
        "model": AI_REPORT_MODEL,
        "store": False,
        "max_output_tokens": 1200,
        "instructions": (
            "Write a short stock report in simple English. Use short sentences. Keep market "
            "slang only when it is precise. No hype, filler, or generic market commentary. "
            "Use only the supplied evidence. "
            "Do not invent news, prices, order-book data, or social data. Do not recommend "
            "buying or selling. Separate verified catalysts from risks and unknowns."
        ),
        "input": json.dumps(evidence, separators=(",", ":")),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "alpha_report",
                "strict": True,
                "schema": schema,
            }
        },
    }
    api_request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {AI_REPORT_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(api_request, timeout=45) as response:  # noqa: S310
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        message = exc.read(1000).decode(errors="replace")
        raise RuntimeError(f"AI report request failed ({exc.code}): {message}") from exc
    output_text = ""
    for item in result.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "refusal":
                raise RuntimeError("AI report request was refused")
            if content.get("type") == "output_text":
                output_text += str(content.get("text") or "")
    report = json.loads(output_text)
    if not isinstance(report, dict) or not all(key in report for key in schema["required"]):
        raise RuntimeError("AI report response was incomplete")
    return report


def refresh_alpha_report() -> dict[str, Any]:
    board = alpha_board_data("system")
    if not board["rows"]:
        return {"status": "idle", "reason": "no_alpha_leader"}
    leader = board["rows"][0]
    ticker = leader["ticker"]
    with connection() as db:
        state_row = db.execute(
            "SELECT value FROM worker_state WHERE key='alpha_report_leader'"
        ).fetchone()
    state = json.loads(state_row["value"]) if state_row else {}
    if state.get("ticker") != ticker:
        worker_state("alpha_report_leader", json.dumps({"ticker": ticker, "since": iso()}))
        return {"status": "waiting", "ticker": ticker}
    try:
        stable_since = datetime.fromisoformat(state["since"])
    except (KeyError, TypeError, ValueError):
        stable_since = now()
    if now() - stable_since < timedelta(minutes=3):
        return {"status": "waiting", "ticker": ticker}
    if not AI_REPORT_API_KEY:
        worker_state("alpha_report_last_error", "OPENAI_API_KEY is not configured")
        return {"status": "provider_missing", "ticker": ticker}
    evidence_key, evidence = _alpha_evidence(ticker, leader["heart_count"])
    report_id = secrets.token_urlsafe(10)
    with connection() as db:
        existing = db.execute(
            "SELECT * FROM alpha_reports WHERE ticker=? AND evidence_key=?",
            (ticker, evidence_key),
        ).fetchone()
        if existing and existing["status"] == "complete":
            return {"status": "current", "ticker": ticker, "report_id": existing["id"]}
        if existing and existing["status"] == "running":
            return {"status": "running", "ticker": ticker, "report_id": existing["id"]}
        if existing and existing["status"] == "failed":
            try:
                failed_at = datetime.fromisoformat(existing["updated_at"])
            except (TypeError, ValueError):
                failed_at = now() - timedelta(hours=1)
            if now() - failed_at < timedelta(minutes=15):
                return {"status": "cooldown", "ticker": ticker, "report_id": existing["id"]}
        if existing:
            report_id = existing["id"]
            db.execute(
                "UPDATE alpha_reports SET status='running',error=NULL,updated_at=? WHERE id=?",
                (iso(), report_id),
            )
        else:
            db.execute(
                """
                INSERT INTO alpha_reports(
                    id,ticker,evidence_key,status,model,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (report_id, ticker, evidence_key, "running", AI_REPORT_MODEL, iso(), iso()),
            )
    try:
        report = _generate_alpha_report(evidence)
        sources = [item["url"] for item in evidence["filings"] if item.get("url")]
        with connection() as db:
            db.execute(
                """
                UPDATE alpha_reports SET status='complete',headline=?,summary=?,
                    catalysts_json=?,risks_json=?,watch_json=?,sources_json=?,
                    error=NULL,updated_at=? WHERE id=?
                """,
                (
                    str(report["headline"])[:180],
                    str(report["summary"])[:1200],
                    json.dumps(report["catalysts"][:6]),
                    json.dumps(report["risks"][:6]),
                    json.dumps(report["watch"][:6]),
                    json.dumps(sources[:8]),
                    iso(),
                    report_id,
                ),
            )
        worker_state("alpha_report_last_error", "")
        return {"status": "complete", "ticker": ticker, "report_id": report_id}
    except Exception as exc:
        with connection() as db:
            db.execute(
                "UPDATE alpha_reports SET status='failed',error=?,updated_at=? WHERE id=?",
                (str(exc)[:1000], iso(), report_id),
            )
        worker_state("alpha_report_last_error", str(exc)[:500])
        return {"status": "failed", "ticker": ticker, "error": str(exc)}


async def alpha_report_worker() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            await run_in_threadpool(refresh_alpha_report)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            worker_state("alpha_report_last_error", str(exc)[:500])
        await asyncio.sleep(60)


def pulse_notification_data(profile: str) -> dict[str, Any]:
    payload = pulse_data(limit=50, profile=profile)
    cutoff = now() - PULSE_NOTIFICATION_WINDOW
    entries: list[dict[str, Any]] = []
    for row in payload["rows"]:
        entered_at = row.get("entered_at")
        if row.get("novelty_state") != "unseen" or row.get("notified_at") or not entered_at:
            continue
        try:
            entered = datetime.fromisoformat(str(entered_at))
        except ValueError:
            continue
        if entered.tzinfo is None:
            entered = entered.replace(tzinfo=UTC)
        if entered < cutoff:
            continue
        entries.append(
            {
                "ticker": row["ticker"],
                "company": row.get("company") or row["ticker"],
                "stage": row.get("stage") or "WATCH",
                "price": row.get("price"),
                "change_pct": row.get("change_pct"),
                "entered_at": entered_at,
                "coin_label": row.get("coin_label") or str(row["ticker"])[:2],
                "coin_tone": row.get("coin_tone", 0),
            }
        )
    entries.sort(key=lambda item: str(item["entered_at"]), reverse=True)
    return {"entries": entries[:5], "checked_at": iso()}


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    return templates.TemplateResponse(
        request=request,
        name="pulse.html",
        context=page_context(
            request,
            runner_session,
            pulse=pulse_data(limit=20, profile=profile),
            active_tab="pulse",
        ),
    )


@app.get("/api/pulse")
def pulse_api(
    request: Request,
    offset: int = 0,
    limit: int = 20,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    enforce_rate(request, "pulse", limit=180, seconds=60)
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    return JSONResponse(pulse_data(offset=offset, limit=limit, profile=profile))


@app.get("/api/pulse/notifications")
def pulse_notifications_api(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    enforce_rate(request, "pulse-notifications", limit=120, seconds=60)
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    return JSONResponse(pulse_notification_data(profile))


@app.post("/api/pulse/seen")
def pulse_seen_api(
    payload: PulseAttentionPayload,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    require_origin(request)
    enforce_rate(request, "pulse-seen", limit=120, seconds=60)
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    return {"updated": _write_pulse_attention(profile, payload.entries, "seen")}


@app.post("/api/pulse/notified")
def pulse_notified_api(
    payload: PulseAttentionPayload,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    require_origin(request)
    enforce_rate(request, "pulse-notified", limit=120, seconds=60)
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    return {"updated": _write_pulse_attention(profile, payload.entries, "notified")}


@app.get("/api/pulse/charts")
async def pulse_charts_api(request: Request) -> JSONResponse:
    enforce_rate(request, "pulse-charts", limit=20, seconds=60)
    tickers = [row["ticker"] for row in pulse_data(limit=20)["rows"]]
    payload = await run_in_threadpool(ticker_charts_payload, tickers)
    return JSONResponse(payload)


def _clean_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper().replace(".", "-")
    if not TICKER_RE.fullmatch(normalized):
        raise HTTPException(404, "Ticker not found")
    return normalized


def _ticker_exists(ticker: str) -> bool:
    with connection() as db:
        return (
            db.execute(
                """
                SELECT 1 FROM sec_companies WHERE ticker=?
                UNION SELECT 1 FROM sec_filings WHERE ticker=?
                UNION SELECT 1 FROM scan_snapshots WHERE ticker=?
                UNION SELECT 1 FROM market_events WHERE ticker=? LIMIT 1
                """,
                (ticker, ticker, ticker, ticker),
            ).fetchone()
            is not None
        )


def ticker_detail_data(ticker: str) -> dict[str, Any] | None:
    with connection() as db:
        filings = db.execute(
            """
            SELECT f.*,o.return_1h_pct,o.return_1d_pct,o.return_5d_pct,
                   o.observed_1h_at,o.observed_1d_at,o.observed_5d_at
            FROM sec_filings f
            LEFT JOIN sec_outcomes o ON o.accession=f.accession
            WHERE f.ticker=? AND f.created_at>?
            ORDER BY f.filed_at DESC LIMIT 12
            """,
            (ticker, iso(now() - timedelta(days=30))),
        ).fetchall()
        company = db.execute(
            "SELECT name,exchange FROM sec_companies WHERE ticker=? LIMIT 1", (ticker,)
        ).fetchone()
        snapshot = db.execute(
            """
            SELECT s.*,COALESCE(o.return_60m_pct,o.return_1h_pct) AS scan_return_1h_pct,
                   o.return_1d_pct AS scan_return_1d_pct,
                   o.return_5d_pct AS scan_return_5d_pct,
                   o.barrier_label,o.max_favorable_pct,o.max_adverse_pct
            FROM scan_snapshots s
            LEFT JOIN scan_outcomes o ON o.snapshot_id=s.id
            WHERE s.ticker=?
            ORDER BY captured_at DESC LIMIT 1
            """,
            (ticker,),
        ).fetchone()
        external_rows = db.execute(
            """
            SELECT source,ticker,event_type,status,event_at,source_url,payload_json
            FROM market_events WHERE ticker=? AND event_at>?
            ORDER BY event_at DESC,last_collected_at DESC LIMIT 30
            """,
            (ticker, iso(now() - timedelta(days=3))),
        ).fetchall()

    events = []
    for row in filings:
        event = _intelligence_evidence(dict(row))
        event["pulse_label"] = _pulse_label(event)
        event["event_at"] = event["filed_at"]
        events.append(event)
    if not events and snapshot is None and company is None and not external_rows:
        return None
    if snapshot is not None:
        current = dict(snapshot)
        current.update(
            {
                "ticker": ticker,
                "kind": current.get("catalyst_kind") or "No recent SEC catalyst",
                "sentiment": current.get("catalyst_sentiment") or "gap",
                "event_at": current["captured_at"],
                "return_1h_pct": current.get("scan_return_1h_pct"),
                "return_1d_pct": current.get("scan_return_1d_pct"),
                "return_5d_pct": current.get("scan_return_5d_pct"),
                "signals": _json_list(current.get("signals_json")),
                "risks": _json_list(current.get("risks_json")),
                "issuer_risk": json.loads(current.get("issuer_risk_json") or "{}"),
                "source": "market",
            }
        )
    elif events:
        current = {**events[0], "signals": [], "risks": [], "source": "sec"}
    else:
        current = {
            "ticker": ticker,
            "price": None,
            "change_pct": None,
            "score": 0,
            "kind": "Watching for intelligence",
            "sentiment": "neutral",
            "event_at": None,
            "return_1h_pct": None,
            "return_1d_pct": None,
            "return_5d_pct": None,
            "signals": [],
            "risks": [],
            "source": "quiet",
        }
    pressure = _market_trade_pressure(ticker)
    external = _external_event_context([dict(row) for row in external_rows])
    base_rates = matched_market_base_rates(current)
    return {
        "ticker": ticker,
        "company": company["name"] if company else current.get("company", ticker),
        "exchange": company["exchange"] if company else "Listed US stock",
        "coin_label": ticker[:2],
        "coin_tone": _coin_tone(ticker),
        "current": current,
        "events": events,
        "external_events": [
            {**dict(row), "payload": _event_payload(dict(row))} for row in external_rows
        ],
        "external_context": external,
        "base_rates": base_rates,
        "trade_pressure": pressure,
        "kol_calls": calls_for_ticker(ticker),
        "evidence_gate": _evidence_gate(
            current,
            events,
            pressure,
            external_context=external,
            base_rates=base_rates,
        ),
        "can_publish": bool(
            snapshot is not None and str(snapshot["captured_at"]) > iso(now() - timedelta(hours=2))
        ),
    }


def _chart_points(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    close = pd.Series(dtype="float64")
    for column in frame.columns:
        if str(column).lower().replace(" ", "") == "close":
            close = pd.to_numeric(frame[column], errors="coerce").dropna()
            break
    if close.empty:
        return []
    step = max(1, len(close) // 100)
    sampled = close.iloc[::step]
    if sampled.index[-1] != close.index[-1]:
        sampled = pd.concat([sampled, close.iloc[[-1]]])
    points = [
        {"time": pd.Timestamp(stamp).isoformat(), "price": round(float(price), 6)}
        for stamp, price in sampled.items()
    ]
    return points


def _chart_topic(ticker: str) -> str:
    return f"market:bars:{ticker}:5m"


def _pulse_entry_markers(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Return the latest real Pulse entry inside the chart window.

    A ticker can remain in several scans. Only a change from absent to present is
    an entry, so normal scan refreshes do not move the marker forward.
    """

    requested = list(dict.fromkeys(str(ticker).upper() for ticker in tickers))[:50]
    if not requested:
        return {}
    cutoff = iso(now() - timedelta(days=6))
    placeholders = ",".join("?" for _ in requested)
    with connection() as db:
        runs = db.execute(
            """
            SELECT id,captured_at FROM scan_runs
            WHERE candidate_rows>0 AND captured_at>=?
            ORDER BY captured_at,id
            """,
            (cutoff,),
        ).fetchall()
        prior_run = db.execute(
            """
            SELECT id,captured_at FROM scan_runs
            WHERE candidate_rows>0 AND captured_at<?
            ORDER BY captured_at DESC,id DESC LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
        snapshot_rows = db.execute(
            f"""
            SELECT s.scan_run_id,s.ticker,s.captured_at,s.price
            FROM scan_snapshots s
            JOIN scan_runs r ON r.id=s.scan_run_id
            WHERE r.candidate_rows>0 AND r.captured_at>=?
              AND s.ticker IN ({placeholders})
            """,
            (cutoff, *requested),
        ).fetchall()
        prior_rows = (
            db.execute(
                f"""
                SELECT scan_run_id,ticker,captured_at,price FROM scan_snapshots
                WHERE scan_run_id=? AND ticker IN ({placeholders})
                """,
                (prior_run["id"], *requested),
            ).fetchall()
            if prior_run
            else []
        )

    snapshots_by_run: dict[str, dict[str, dict[str, Any]]] = {}
    for raw in snapshot_rows:
        snapshot = dict(raw)
        snapshots_by_run.setdefault(str(snapshot["scan_run_id"]), {})[str(snapshot["ticker"])] = (
            snapshot
        )
    previous = {str(row["ticker"]) for row in prior_rows}
    entries: dict[str, dict[str, Any]] = {}
    for run in runs:
        current = snapshots_by_run.get(str(run["id"]), {})
        for ticker, snapshot in current.items():
            if ticker not in previous:
                entries[ticker] = {
                    "type": "pulse_entry",
                    "category": "Pulse",
                    "label": "Entered Pulse",
                    "time": str(snapshot["captured_at"]),
                    "price": snapshot.get("price"),
                    "tone": "pulse",
                    "url": None,
                }
        previous = set(current)
    return entries


def _chart_annotations(tickers: list[str]) -> dict[str, list[dict[str, Any]]]:
    requested = list(dict.fromkeys(str(ticker).upper() for ticker in tickers))[:50]
    if not requested:
        return {}
    annotations: dict[str, list[dict[str, Any]]] = {ticker: [] for ticker in requested}
    for ticker, marker in _pulse_entry_markers(requested).items():
        if ticker in annotations:
            annotations[ticker].append(marker)

    cutoff = iso(now() - timedelta(days=6))
    placeholders = ",".join("?" for _ in requested)
    with connection() as db:
        filing_rows = db.execute(
            f"""
            SELECT ticker,form,kind,sentiment,filed_at,filing_url
            FROM sec_filings
            WHERE ticker IN ({placeholders}) AND filed_at>=?
            ORDER BY filed_at
            """,
            (*requested, cutoff),
        ).fetchall()
        market_rows = db.execute(
            f"""
            SELECT source,ticker,event_type,status,event_at,source_url,payload_json
            FROM market_events
            WHERE ticker IN ({placeholders}) AND event_at>=?
            ORDER BY event_at
            """,
            (*requested, cutoff),
        ).fetchall()

    for raw in filing_rows:
        event = dict(raw)
        ticker = str(event["ticker"])
        form = str(event.get("form") or "filing")
        kind = str(event.get("kind") or "New filing")
        annotations[ticker].append(
            {
                "type": "edgar_filing",
                "category": "EDGAR",
                "label": f"EDGAR {form} · {kind}"[:140],
                "time": str(event["filed_at"]),
                "tone": str(event.get("sentiment") or "neutral"),
                "url": event.get("filing_url"),
            }
        )

    for raw in market_rows:
        event = dict(raw)
        ticker = str(event["ticker"])
        event_type = str(event.get("event_type") or "market_event")
        payload = _event_payload(event)
        tone = "neutral"
        category = "Event"
        annotation_type = event_type
        if event_type == "social_spike":
            mentions = int(payload.get("mention_count") or 0)
            label = f"Social spike · {mentions} mention{'s' if mentions != 1 else ''}"
            category = "Social"
            annotation_type = "media_spike"
            tone = "media"
        elif event_type == "news_article":
            title = str(payload.get("title") or "Company news")
            label = f"News · {title[:110]}"
            category = "News"
            tone = "media"
        elif event_type == "trading_halt":
            label = f"Trading halt · {event.get('status') or 'detected'}"
            category = "Halt"
            tone = "risk"
        else:
            label = f"{event_type.replace('_', ' ').title()} · {event.get('status') or 'detected'}"
        annotations[ticker].append(
            {
                "type": annotation_type,
                "category": category,
                "label": label[:140],
                "time": str(event["event_at"]),
                "tone": tone,
                "url": event.get("source_url"),
                "source": event.get("source"),
            }
        )

    for ticker, items in annotations.items():
        deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in items:
            key = (str(item["type"]), str(item["time"]), str(item["label"]))
            deduplicated[key] = item
        annotations[ticker] = sorted(deduplicated.values(), key=lambda item: str(item["time"]))[
            -32:
        ]
    return annotations


def ticker_chart_snapshots(tickers: list[str]) -> dict[str, TopicSnapshot]:
    requested = list(dict.fromkeys(tickers))[:50]
    topic_to_ticker = {_chart_topic(ticker): ticker for ticker in requested}

    def produce(topics: tuple[str, ...]) -> dict[str, TopicUpdate]:
        symbols = [topic_to_ticker[topic] for topic in topics]
        placeholders = ",".join("?" for _ in symbols)
        cutoff = iso(now() - timedelta(days=7))
        with connection() as db:
            rows = db.execute(
                f"""
                SELECT ticker,bar_time,close,source,last_collected_at
                FROM market_bars
                WHERE source='yahoo' AND interval='5m'
                  AND ticker IN ({placeholders})
                  AND bar_time>=? AND close IS NOT NULL
                ORDER BY ticker,bar_time
                """,
                (*symbols, cutoff),
            ).fetchall()

        bars: dict[str, list[dict[str, Any]]] = {symbol: [] for symbol in symbols}
        for row in rows:
            bars[str(row["ticker"])].append(dict(row))

        updates: dict[str, TopicUpdate] = {}
        for topic in topics:
            ticker = topic_to_ticker[topic]
            ticker_bars = bars.get(ticker) or []
            if not ticker_bars:
                continue
            step = max(1, len(ticker_bars) // 100)
            sampled = ticker_bars[::step]
            if sampled[-1]["bar_time"] != ticker_bars[-1]["bar_time"]:
                sampled.append(ticker_bars[-1])
            points = [
                {"time": str(row["bar_time"]), "price": round(float(row["close"]), 6)}
                for row in sampled
            ]
            last = ticker_bars[-1]
            as_of = datetime.fromisoformat(str(last["bar_time"]))
            collected_at = datetime.fromisoformat(str(last["last_collected_at"]))
            updates[topic] = TopicUpdate(
                data=points,
                source=str(last["source"]),
                as_of=as_of,
                collected_at=collected_at,
                delayed=True,
            )
        return updates

    snapshots = MARKET_TOPICS.get_many(
        list(topic_to_ticker),
        policy=CHART_TOPIC_POLICY,
        producer=produce,
    )
    return {ticker: snapshots[topic] for topic, ticker in topic_to_ticker.items()}


def ticker_charts_payload(tickers: list[str]) -> dict[str, Any]:
    requested = list(dict.fromkeys(tickers))[:50]
    snapshots = ticker_chart_snapshots(tickers)
    return {
        "charts": {
            ticker: snapshot.data if isinstance(snapshot.data, list) else []
            for ticker, snapshot in snapshots.items()
        },
        "freshness": {ticker: snapshot.metadata() for ticker, snapshot in snapshots.items()},
        "annotations": _chart_annotations(requested),
    }


def ticker_charts_data(tickers: list[str]) -> dict[str, list[dict[str, Any]]]:
    return ticker_charts_payload(tickers)["charts"]


def ticker_chart_data(ticker: str) -> list[dict[str, Any]]:
    return ticker_charts_data([ticker]).get(ticker, [])


@app.get("/t/{ticker}", response_class=HTMLResponse)
def ticker_page(
    ticker: str,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    normalized = _clean_ticker(ticker)
    detail = ticker_detail_data(normalized)
    if detail is None:
        raise HTTPException(404, "Ticker not found")
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    _record_activity(profile, normalized, "view")
    pulse_entry = _pulse_entry_markers([normalized]).get(normalized)
    if pulse_entry:
        _write_pulse_attention(
            profile,
            [
                PulseAttentionItem(
                    ticker=normalized,
                    entered_at=str(pulse_entry["time"]),
                )
            ],
            "inspected",
        )
    comments = comments_for_ticker(normalized)
    return templates.TemplateResponse(
        request=request,
        name="ticker.html",
        context=page_context(
            request,
            runner_session,
            detail=detail,
            heart=heart_state(normalized, profile),
            reaction=reaction_state(normalized, profile),
            comments=comments,
            comment_count=comment_count_for_ticker(normalized),
            latest_commission=(latest_commission(user["id"], normalized) if user else None),
            active_tab="pulse",
        ),
    )


@app.get("/api/t/{ticker}/chart")
async def ticker_chart_api(ticker: str, request: Request) -> JSONResponse:
    enforce_rate(request, "ticker-chart", limit=90, seconds=60)
    normalized = _clean_ticker(ticker)
    if not _ticker_exists(normalized):
        raise HTTPException(404, "Ticker not found")
    payload = await run_in_threadpool(ticker_charts_payload, [normalized])
    return JSONResponse(
        {
            "ticker": normalized,
            "points": payload["charts"].get(normalized, []),
            "freshness": payload["freshness"].get(normalized),
            "annotations": payload["annotations"].get(normalized, []),
        }
    )


@app.get("/api/t/{ticker}/pressure")
async def ticker_pressure_api(ticker: str, request: Request) -> JSONResponse:
    enforce_rate(request, "ticker-pressure", limit=60, seconds=60)
    normalized = _clean_ticker(ticker)
    if not _ticker_exists(normalized):
        raise HTTPException(404, "Ticker not found")
    await run_in_threadpool(ticker_chart_data, normalized)
    pressure = await run_in_threadpool(_market_trade_pressure, normalized)
    detail = ticker_detail_data(normalized)
    gate = (
        _evidence_gate(
            detail["current"],
            detail["events"],
            pressure,
            external_context=detail["external_context"],
            base_rates=detail["base_rates"],
        )
        if detail
        else None
    )
    return JSONResponse({"ticker": normalized, "pressure": pressure, "evidence_gate": gate})


def _ticker_summary(ticker: str) -> dict[str, Any] | None:
    detail = ticker_detail_data(ticker)
    if not detail:
        return None
    external = detail["external_context"]
    external_label = _external_event_label(external)
    current = dict(detail["current"])
    filing = detail["events"][0] if detail["events"] else None
    source = str(current.get("source") or "quiet")
    if external_label:
        pulse_label = external_label[0]
        context_source = external_label[1]
    elif source == "market":
        pulse_label = _market_pulse_label(current, filing)
        context_source = source
    elif filing:
        pulse_label = _pulse_label(filing)
        context_source = source
    else:
        pulse_label = "Quiet"
        context_source = source
    return {
        **current,
        "ticker": ticker,
        "company": detail["company"],
        "exchange": detail["exchange"],
        "coin_label": detail["coin_label"],
        "coin_tone": detail["coin_tone"],
        "source": context_source,
        "pulse_label": pulse_label,
        "event_count": len(detail["events"]) + int(external["normalized_event_count"]),
        "event_at": current.get("event_at"),
        "sentiment": filing.get("sentiment") if filing else current.get("sentiment", "gap"),
        "evidence_gate": detail["evidence_gate"],
        "filing_url": (
            external_label[2] if external_label else filing.get("filing_url") if filing else None
        ),
        "news_count": external["news_count"],
        "latest_news": external.get("latest_news"),
        "external_social_mentions": external["social_mentions"],
        "external_social_engagement": external["social_engagement"],
        "active_market_event": external.get("active_halt"),
    }


def _activity_scores(profile: str, user_id: str | None = None) -> dict[str, float]:
    cutoff = now() - timedelta(days=30)
    scores: dict[str, float] = {}
    with connection() as db:
        rows = db.execute(
            """
            SELECT ticker,event_type,weight,created_at FROM activity_events
            WHERE profile_id=? AND created_at>? ORDER BY created_at DESC LIMIT 1000
            """,
            (profile, iso(cutoff)),
        ).fetchall()
        hearts = db.execute(
            "SELECT ticker FROM ticker_hearts WHERE profile_id=? AND active=1",
            (profile,),
        ).fetchall()
        legacy = (
            db.execute("SELECT ticker FROM watches WHERE user_id=?", (user_id,)).fetchall()
            if user_id
            else []
        )
    for row in rows:
        try:
            age_seconds = max(
                0.0, (now() - datetime.fromisoformat(row["created_at"])).total_seconds()
            )
        except (TypeError, ValueError):
            age_seconds = 30 * 86400
        decay = math.exp(-age_seconds / (7 * 86400))
        scores[row["ticker"]] = scores.get(row["ticker"], 0.0) + float(row["weight"]) * decay
    for row in hearts:
        scores[row["ticker"]] = scores.get(row["ticker"], 0.0) + 8.0
    for row in legacy:
        scores[row["ticker"]] = scores.get(row["ticker"], 0.0) + 2.0
    return scores


def _radar_market_summaries(tickers: list[str]) -> dict[str, dict[str, Any]]:
    """Load the small amount of ticker context Radar actually renders."""

    requested = list(dict.fromkeys(str(ticker).upper() for ticker in tickers))[:40]
    if not requested:
        return {}
    placeholders = ",".join("?" for _ in requested)
    with connection() as db:
        company_rows = db.execute(
            f"""
            SELECT ticker,name,exchange FROM sec_companies
            WHERE ticker IN ({placeholders})
            """,
            requested,
        ).fetchall()
        snapshot_rows = db.execute(
            f"""
            WITH ranked AS (
                SELECT s.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker ORDER BY captured_at DESC
                       ) AS radar_position
                FROM scan_snapshots s WHERE ticker IN ({placeholders})
            )
            SELECT * FROM ranked WHERE radar_position=1
            """,
            requested,
        ).fetchall()
        filing_rows = db.execute(
            f"""
            WITH ranked AS (
                SELECT f.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY ticker ORDER BY filed_at DESC,score DESC
                       ) AS radar_position
                FROM sec_filings f
                WHERE ticker IN ({placeholders}) AND created_at>?
            )
            SELECT * FROM ranked WHERE radar_position=1
            """,
            (*requested, iso(now() - timedelta(days=30))),
        ).fetchall()

    companies = {str(row["ticker"]): dict(row) for row in company_rows}
    snapshots = {str(row["ticker"]): dict(row) for row in snapshot_rows}
    filings = {str(row["ticker"]): dict(row) for row in filing_rows}
    summaries: dict[str, dict[str, Any]] = {}
    for ticker in requested:
        company = companies.get(ticker, {})
        summary = snapshots.get(ticker) or filings.get(ticker, {})
        summary.pop("radar_position", None)
        summaries[ticker] = {
            **summary,
            "ticker": ticker,
            "company": company.get("name") or summary.get("company") or ticker,
            "exchange": company.get("exchange") or "Listed US stock",
            "coin_label": ticker[:2],
            "coin_tone": _coin_tone(ticker),
        }
    return summaries


def _radar_base_data_uncached() -> list[dict[str, Any]]:
    cutoff = iso(now() - timedelta(days=3))
    with connection() as db:
        filing_rows = db.execute(
            """
            WITH ranked AS (
                SELECT f.*,o.return_1h_pct,o.return_1d_pct,o.return_5d_pct,
                       COUNT(*) OVER (PARTITION BY f.ticker) AS radar_event_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY f.ticker
                           ORDER BY f.filed_at DESC,f.score DESC
                       ) AS radar_position
                FROM sec_filings f
                LEFT JOIN sec_outcomes o ON o.accession=f.accession
                WHERE f.created_at>?
                  AND NOT (
                      COALESCE(f.sentiment,'')='neutral' AND COALESCE(f.score,0)<40
                  )
            )
            SELECT * FROM ranked WHERE radar_position=1
            ORDER BY filed_at DESC,score DESC LIMIT 40
            """,
            (cutoff,),
        ).fetchall()
        market_event_rows = db.execute(
            """
            WITH ranked AS (
                SELECT m.*,
                       COUNT(*) OVER (PARTITION BY m.ticker) AS radar_event_count,
                       ROW_NUMBER() OVER (
                           PARTITION BY m.ticker
                           ORDER BY m.event_at DESC,m.last_collected_at DESC
                       ) AS radar_position
                FROM market_events m
                WHERE m.event_at>? AND COALESCE(m.ticker,'')!=''
            )
            SELECT * FROM ranked WHERE radar_position=1
            ORDER BY event_at DESC,last_collected_at DESC LIMIT 40
            """,
            (cutoff,),
        ).fetchall()
    events: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for raw in filing_rows:
        event = dict(raw)
        event_count = int(event.pop("radar_event_count", 1))
        event.pop("radar_position", None)
        event = _intelligence_evidence(event)
        ticker = event["ticker"]
        counts[ticker] = counts.get(ticker, 0) + event_count
        events.append(
            {
                **event,
                "coin_label": ticker[:2],
                "coin_tone": _coin_tone(ticker),
                "pulse_label": _pulse_label(event),
                "source": "sec",
                "section": "events",
                "event_at": event["filed_at"],
                "attention_score": float(event.get("score") or 0),
                "filing_url": event.get("filing_url"),
            }
        )
    market_summaries = _radar_market_summaries(
        [str(row["ticker"]) for row in market_event_rows]
    )
    for raw in market_event_rows:
        event = dict(raw)
        event_count = int(event.pop("radar_event_count", 1))
        event.pop("radar_position", None)
        ticker = str(event.get("ticker") or "").upper()
        if not ticker:
            continue
        counts[ticker] = counts.get(ticker, 0) + event_count
        summary = market_summaries.get(ticker, {})
        payload = _event_payload(event)
        event_type_key = str(event.get("event_type") or "market_event")
        event_type = event_type_key.replace("_", " ")
        status = str(event.get("status") or "updated")
        active = False
        score = 55.0
        sentiment = "neutral"
        label = f"{event_type.title()} · {status}"
        if event_type_key == "trading_halt":
            active = bool(_external_event_context([event]).get("active_halt"))
            score = 95.0 if active else 55.0
            sentiment = "risk" if active else "neutral"
            label = f"Trading halt · {status}"
        elif event_type_key == "news_article":
            stamp = _event_timestamp(event)
            age_hours = max(0.0, (now() - stamp).total_seconds() / 3600) if stamp else 24.0
            score = round(52.0 + max(0.0, 12.0 - age_hours / 2), 2)
            title = str(payload.get("title") or "New company coverage")
            label = f"News · {title[:96]}"
        elif event_type_key == "social_spike":
            mentions = int(payload.get("mention_count") or 0)
            engagement = int(payload.get("engagement_count") or 0)
            score = round(
                min(80.0, 45.0 + mentions * 2.0 + math.log2(engagement + 1) * 2.0),
                2,
            )
            network = str(payload.get("network_label") or "Social")
            noun = "cashtag mention" if network == "Bluesky" else "mention"
            label = f"{network} · {mentions} {noun}{'s' if mentions != 1 else ''}"
        else:
            active = status.lower() not in {"resolved", "closed", "published"}
            score = 75.0 if active else 55.0
            sentiment = "risk" if active else "neutral"
        events.append(
            {
                **summary,
                "ticker": ticker,
                "company": summary.get("company") or ticker,
                "coin_label": summary.get("coin_label") or ticker[:2],
                "coin_tone": summary.get("coin_tone", _coin_tone(ticker)),
                "kind": event_type.title(),
                "sentiment": sentiment,
                "score": score,
                "pulse_label": label,
                "source": event.get("source") or "market_event",
                "section": "events",
                "event_at": event["event_at"],
                "attention_score": score,
                "filing_url": event.get("source_url"),
                "external_social_mentions": int(payload.get("mention_count") or 0),
                "external_social_engagement": int(payload.get("engagement_count") or 0),
            }
        )

    latest_by_ticker: dict[str, dict[str, Any]] = {}
    for item in events:
        ticker = item["ticker"]
        current = latest_by_ticker.get(ticker)
        if current is None or str(item["event_at"]) > str(current["event_at"]):
            latest_by_ticker[ticker] = item
    output = list(latest_by_ticker.values())
    for item in output:
        ticker = item["ticker"]
        item["event_count"] = counts[ticker]
        item["evidence_gate"] = _evidence_gate(item, [item])
    output.sort(
        key=lambda row: (
            str(row.get("event_at") or ""),
            float(row.get("attention_score") or row.get("score") or 0),
        ),
        reverse=True,
    )
    return output[:20]


def _refresh_radar_base(cache_key: str) -> None:
    try:
        output = _radar_base_data_uncached()
        with RADAR_DATA_LOCK:
            RADAR_DATA_CACHE[cache_key] = (
                time.monotonic() + RADAR_CACHE_TTL_SECONDS,
                output,
            )
    except Exception:
        LOG.exception("Radar cache refresh failed")
    finally:
        with RADAR_DATA_LOCK:
            RADAR_DATA_REFRESHING.discard(cache_key)


def _radar_base_data() -> list[dict[str, Any]]:
    cache_key = str(runner_db.DATABASE_PATH)
    current = time.monotonic()
    with RADAR_DATA_LOCK:
        cached = RADAR_DATA_CACHE.get(cache_key)
        if cached and current < cached[0]:
            return cached[1]
        if cached:
            if cache_key not in RADAR_DATA_REFRESHING:
                RADAR_DATA_REFRESHING.add(cache_key)
                threading.Thread(
                    target=_refresh_radar_base,
                    args=(cache_key,),
                    daemon=True,
                    name="radar-cache-refresh",
                ).start()
            return cached[1]
        output = _radar_base_data_uncached()
        if len(RADAR_DATA_CACHE) >= 8 and cache_key not in RADAR_DATA_CACHE:
            oldest_key = min(RADAR_DATA_CACHE, key=lambda key: RADAR_DATA_CACHE[key][0])
            RADAR_DATA_CACHE.pop(oldest_key, None)
        RADAR_DATA_CACHE[cache_key] = (current + RADAR_CACHE_TTL_SECONDS, output)
        return output


def radar_data(
    user_id: str | None = None,
    *,
    visitor_id: str | None = None,
    mark_seen: bool = False,
) -> list[dict[str, Any]]:
    profile = f"u:{user_id}" if user_id else f"v:{visitor_id or 'guest'}"
    output = [dict(row) for row in _radar_base_data()]
    with connection() as db:
        seen = {
            row["ticker"]: row["last_seen_at"]
            for row in db.execute(
                "SELECT ticker,last_seen_at FROM radar_seen WHERE profile_id=?", (profile,)
            ).fetchall()
        }
    for item in output:
        ticker = item["ticker"]
        event_at = item.get("event_at")
        item["has_update"] = bool(event_at and (not seen.get(ticker) or event_at > seen[ticker]))
    if mark_seen and output:
        seen_at = iso()
        with connection() as db:
            db.executemany(
                """
                INSERT INTO radar_seen(profile_id,ticker,last_seen_at) VALUES(?,?,?)
                ON CONFLICT(profile_id,ticker) DO UPDATE SET last_seen_at=excluded.last_seen_at
                """,
                [(profile, row["ticker"], seen_at) for row in output],
            )
    return output


async def request_cache_warmer() -> None:
    """Fill request caches shortly after startup without delaying health checks."""

    await asyncio.sleep(1)
    for builder in (_pulse_base_data, _radar_base_data, _alpha_base_data):
        try:
            await asyncio.to_thread(builder)
        except Exception:
            LOG.exception("Startup request cache warm failed")


@app.get("/radar", response_class=HTMLResponse)
def radar_page(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    user = current_user(runner_session)
    if user:
        claim_visitor_profile(request, user)
    return templates.TemplateResponse(
        request=request,
        name="radar.html",
        context=page_context(
            request,
            runner_session,
            watches=radar_data(
                user["id"] if user else None,
                visitor_id=request.state.visitor_id,
                mark_seen=True,
            ),
            active_tab="radar",
        ),
    )


@app.get("/api/radar")
def radar_api(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    enforce_rate(request, "radar", limit=120, seconds=60, subject=profile)
    return JSONResponse(
        {
            "rows": radar_data(
                user["id"] if user else None,
                visitor_id=request.state.visitor_id,
                mark_seen=True,
            ),
            "updated_at": iso(),
        }
    )


@app.get("/api/radar/charts")
async def radar_charts_api(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    enforce_rate(request, "radar-charts", limit=20, seconds=60, subject=profile)
    tickers = [
        row["ticker"]
        for row in radar_data(
            user["id"] if user else None,
            visitor_id=request.state.visitor_id,
        )
    ]
    payload = await run_in_threadpool(ticker_charts_payload, tickers)
    return JSONResponse(payload)


def _known_ticker(ticker: str) -> bool:
    with connection() as db:
        return (
            db.execute(
                """
                SELECT 1 FROM sec_companies WHERE ticker=?
                UNION SELECT 1 FROM sec_filings WHERE ticker=?
                UNION SELECT 1 FROM scan_snapshots WHERE ticker=? LIMIT 1
                """,
                (ticker, ticker, ticker),
            ).fetchone()
            is not None
        )


def _record_activity(
    profile: str,
    ticker: str,
    event_type: str,
    *,
    seconds: int = 0,
) -> None:
    if event_type == "dwell" and seconds < 3:
        return
    weights = {"view": 1.0, "dwell": min(5.0, max(1.0, seconds / 10)), "share": 3.0}
    weight = weights.get(event_type)
    if weight is None:
        return
    with connection() as db:
        if event_type == "view":
            recent = db.execute(
                """
                SELECT 1 FROM activity_events
                WHERE profile_id=? AND ticker=? AND event_type='view' AND created_at>?
                LIMIT 1
                """,
                (profile, ticker, iso(now() - timedelta(minutes=1))),
            ).fetchone()
            if recent:
                return
        db.execute(
            """
            INSERT INTO activity_events(profile_id,ticker,event_type,weight,created_at)
            VALUES(?,?,?,?,?)
            """,
            (profile, ticker, event_type, weight, iso()),
        )


def heart_state(ticker: str, profile: str) -> dict[str, Any]:
    with connection() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM ticker_hearts WHERE ticker=? AND active=1", (ticker,)
        ).fetchone()[0]
        active = db.execute(
            "SELECT active FROM ticker_hearts WHERE profile_id=? AND ticker=?",
            (profile, ticker),
        ).fetchone()
    return {"ticker": ticker, "count": int(count), "hearted": bool(active and active[0])}


def reaction_state(ticker: str, profile: str) -> dict[str, Any]:
    counts = {"bull": 0, "bear": 0}
    with connection() as db:
        rows = db.execute(
            """
            SELECT reaction,COUNT(*) AS reaction_count
            FROM ticker_reactions WHERE ticker=?
            GROUP BY reaction
            """,
            (ticker,),
        ).fetchall()
        selected = db.execute(
            "SELECT reaction FROM ticker_reactions WHERE profile_id=? AND ticker=?",
            (profile, ticker),
        ).fetchone()
    for row in rows:
        reaction = str(row["reaction"])
        if reaction in counts:
            counts[reaction] = int(row["reaction_count"])
    return {
        "ticker": ticker,
        "bull": counts["bull"],
        "bear": counts["bear"],
        "selected": str(selected["reaction"]) if selected else None,
    }


def comment_pseudonym(user_id: str) -> str:
    return pseudonym_candidate(user_id)


def _ensure_comment_pseudonym(database: sqlite3.Connection, user_id: str) -> str:
    existing = database.execute(
        "SELECT pseudonym FROM comment_pseudonyms WHERE user_id=?",
        (user_id,),
    ).fetchone()
    if existing:
        return str(existing["pseudonym"])
    for attempt in range(1_000):
        candidate = pseudonym_candidate(user_id, attempt)
        database.execute(
            """
            INSERT INTO comment_pseudonyms(user_id,pseudonym,created_at)
            VALUES(?,?,?) ON CONFLICT DO NOTHING
            """,
            (user_id, candidate, iso()),
        )
        assigned = database.execute(
            "SELECT pseudonym FROM comment_pseudonyms WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if assigned:
            return str(assigned["pseudonym"])
    raise RuntimeError("Could not assign a unique comment pseudonym")


def _public_comment(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "body": str(row["body"]),
        "created_at": str(row["created_at"]),
        "pseudonym": str(row["pseudonym"]),
    }


def comments_for_ticker(ticker: str, *, limit: int = 50) -> list[dict[str, Any]]:
    bounded_limit = min(50, max(1, limit))
    with connection() as db:
        rows = db.execute(
            """
            SELECT c.id,c.body,c.created_at,p.pseudonym
            FROM ticker_comments c
            JOIN comment_pseudonyms p ON p.user_id=c.user_id
            WHERE c.ticker=? AND c.status='public'
            ORDER BY c.created_at DESC,c.id DESC
            LIMIT ?
            """,
            (ticker, bounded_limit),
        ).fetchall()
    return [_public_comment(row) for row in rows]


def comment_count_for_ticker(ticker: str) -> int:
    with connection() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM ticker_comments WHERE ticker=? AND status='public'",
            (ticker,),
        ).fetchone()[0]
    return int(count)


@app.post("/api/activity")
def record_activity_api(
    payload: ActivityPayload,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    enforce_rate(request, "activity", limit=180, seconds=60, subject=profile)
    ticker = _clean_ticker(payload.ticker)
    if not _known_ticker(ticker):
        raise HTTPException(404, "Ticker not found")
    _record_activity(profile, ticker, payload.event_type, seconds=payload.seconds)
    return JSONResponse({"ok": True})


@app.post("/api/heart/{ticker}")
def toggle_heart(
    ticker: str,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    enforce_rate(request, "heart", limit=40, seconds=60, subject=profile)
    normalized = _clean_ticker(ticker)
    if not _known_ticker(normalized):
        raise HTTPException(404, "Ticker not found")
    timestamp = iso()
    with connection() as db:
        existing = db.execute(
            "SELECT active FROM ticker_hearts WHERE profile_id=? AND ticker=?",
            (profile, normalized),
        ).fetchone()
        active = not bool(existing and existing["active"])
        db.execute(
            """
            INSERT INTO ticker_hearts(profile_id,ticker,active,created_at,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(profile_id,ticker) DO UPDATE SET
                active=excluded.active,updated_at=excluded.updated_at
            """,
            (profile, normalized, int(active), timestamp, timestamp),
        )
    if active:
        _record_activity(profile, normalized, "view")
    with PULSE_DATA_LOCK:
        PULSE_DATA_CACHE.clear()
    with ALPHA_DATA_LOCK:
        ALPHA_DATA_CACHE.clear()
    state = heart_state(normalized, profile)
    return JSONResponse(state)


@app.post("/api/reaction/{ticker}/{reaction}")
def toggle_reaction(
    ticker: str,
    reaction: str,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    normalized_reaction = reaction.strip().lower()
    if normalized_reaction not in {"bull", "bear"}:
        raise HTTPException(400, "Reaction must be bull or bear")
    user = current_user(runner_session)
    profile = claim_visitor_profile(request, user) if user else profile_id(request)
    enforce_rate(request, "reaction", limit=40, seconds=60, subject=profile)
    normalized = _clean_ticker(ticker)
    if not _known_ticker(normalized):
        raise HTTPException(404, "Ticker not found")
    timestamp = iso()
    with connection() as db:
        existing = db.execute(
            "SELECT reaction FROM ticker_reactions WHERE profile_id=? AND ticker=?",
            (profile, normalized),
        ).fetchone()
        if existing and existing["reaction"] == normalized_reaction:
            db.execute(
                "DELETE FROM ticker_reactions WHERE profile_id=? AND ticker=?",
                (profile, normalized),
            )
        else:
            db.execute(
                """
                INSERT INTO ticker_reactions(
                    profile_id,ticker,reaction,created_at,updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(profile_id,ticker) DO UPDATE SET
                    reaction=excluded.reaction,updated_at=excluded.updated_at
                """,
                (profile, normalized, normalized_reaction, timestamp, timestamp),
            )
    return JSONResponse(reaction_state(normalized, profile))


@app.post("/api/comments/{ticker}")
def create_ticker_comment(
    ticker: str,
    payload: TickerCommentPayload,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    user = require_user(runner_session)
    enforce_rate(request, "ticker-comment", limit=20, seconds=3600, subject=user["id"])
    normalized = _clean_ticker(ticker)
    if not _known_ticker(normalized):
        raise HTTPException(404, "Ticker not found")
    body = " ".join(payload.body.split())
    if not body:
        raise HTTPException(400, "Comment cannot be empty")
    comment_id = str(uuid.uuid4())
    created_at = iso()
    with connection() as db:
        pseudonym = _ensure_comment_pseudonym(db, str(user["id"]))
        db.execute(
            """
            INSERT INTO ticker_comments(id,ticker,user_id,body,status,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (comment_id, normalized, user["id"], body, "public", created_at),
        )
        row = db.execute(
            """
            SELECT id,body,created_at,? AS pseudonym
            FROM ticker_comments WHERE id=?
            """,
            (pseudonym, comment_id),
        ).fetchone()
        count = db.execute(
            "SELECT COUNT(*) FROM ticker_comments WHERE ticker=? AND status='public'",
            (normalized,),
        ).fetchone()[0]
    return JSONResponse(
        {"comment": _public_comment(row), "count": int(count)},
        status_code=201,
    )


@app.post("/api/research/{ticker}")
async def commission_research_api(
    ticker: str,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    user = require_user(runner_session)
    enforce_rate(request, "commission-research", limit=6, seconds=3600, subject=user["id"])
    normalized = _clean_ticker(ticker)
    if not _known_ticker(normalized):
        raise HTTPException(404, "Ticker not found")
    openrouter_key = request.headers.get("x-openrouter-key", "").strip()
    if len(openrouter_key) < 20 or len(openrouter_key) > 500:
        raise HTTPException(401, "Connect OpenRouter before commissioning a report.")
    report, created = await run_in_threadpool(
        _create_research_commission,
        user["id"],
        normalized,
    )
    if created:
        await RESEARCH_JOB_QUEUE.put((str(report["id"]), openrouter_key))
    payload = _commission_api_payload(report)
    payload["created"] = created
    return JSONResponse(payload, status_code=202 if payload["status"] == "running" else 200)


@app.get("/api/research/{ticker}")
def research_status_api(
    ticker: str,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    user = require_user(runner_session)
    normalized = _clean_ticker(ticker)
    if not _known_ticker(normalized):
        raise HTTPException(404, "Ticker not found")
    report = latest_commission(user["id"], normalized)
    if not report:
        raise HTTPException(404, "No Flash report found")
    enforce_rate(request, "research-status", limit=180, seconds=600, subject=user["id"])
    payload = _commission_api_payload(report)
    if payload["status"] == "complete":
        with ALPHA_DATA_LOCK:
            ALPHA_DATA_CACHE.clear()
    return JSONResponse(
        payload,
        status_code=200,
    )


@app.get("/research/{public_id}", response_class=HTMLResponse)
def research_report_page(
    public_id: str,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    report = get_commission(public_id)
    if not report:
        raise HTTPException(404, "Research report not found")
    return templates.TemplateResponse(
        request=request,
        name="research_report.html",
        context=page_context(request, runner_session, report=report, active_tab="alpha"),
    )


@app.get("/research/{public_id}/card.png")
def research_report_card(public_id: str) -> Response:
    report = get_commission(public_id)
    if not report:
        raise HTTPException(404, "Research report not found")
    actor = report.get("actor") or {}
    card_label = (
        f"{str(actor.get('display_name') or 'AI').upper()} RESEARCH"
        if actor
        else "RUNNER WATCH RESEARCH"
    )
    model_label = str(actor.get("model_label") or report.get("model") or report["requested_model"])
    ladder_label = f"#{actor.get('ladder_position')} · " if actor else ""
    image = Image.new("RGB", (1200, 630), "#090b0b")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (55, 55, 1145, 575), radius=34, fill="#111514", outline="#57e389", width=3
    )
    draw.text((95, 88), card_label, "#87e8a9", font=font(29, True))
    draw.text((95, 150), f"${report['ticker']}", "#f4f8f6", font=font(84, True))
    headline = "\n".join(textwrap.wrap(str(report["headline"]), width=39)[:3])
    draw.multiline_text((95, 265), headline, fill="#f4f8f6", font=font(37, True), spacing=11)
    draw.text(
        (95, 515),
        f"{ladder_label}{model_label}"[:70] if actor else model_label[:70],
        "#7e8b86",
        font=font(23),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return Response(
        buffer.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public,max-age=3600"},
    )


def intelligence_data() -> dict[str, Any]:
    cutoff = iso(now() - timedelta(days=3))
    with connection() as db:
        rows = db.execute(
            """
            SELECT f.*,o.return_1h_pct,o.return_1d_pct,o.return_5d_pct,
                   o.observed_1h_at,o.observed_1d_at,o.observed_5d_at
            FROM sec_filings f
            LEFT JOIN sec_outcomes o ON o.accession=f.accession
            WHERE f.created_at>?
            ORDER BY f.score DESC, f.filed_at DESC LIMIT 120
            """,
            (cutoff,),
        ).fetchall()
        state_rows = db.execute(
            """
            SELECT key,value,updated_at FROM worker_state
            WHERE key LIKE 'edgar_%' OR key LIKE 'outcomes_%'
            """
        ).fetchall()
    events = [_intelligence_evidence(dict(row)) for row in rows]
    outcome_keys = ("return_1h_pct", "return_1d_pct", "return_5d_pct")
    return {
        "rows": events,
        "state": {row["key"]: row["value"] for row in state_rows},
        "stats": {
            "events": len(events),
            "penny_events": sum(
                1 for row in events if row.get("price") is not None and row["price"] <= 5
            ),
            "labeled_events": sum(
                1 for row in events if any(row.get(key) is not None for key in outcome_keys)
            ),
        },
    }


@app.get("/intelligence", response_class=HTMLResponse)
def intelligence_page() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=308)


@app.get("/api/intelligence")
def intelligence_api() -> JSONResponse:
    return JSONResponse(intelligence_data())


@app.get("/auth/openrouter/callback", response_class=HTMLResponse)
def openrouter_callback(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="openrouter_callback.html",
        context=page_context(request, runner_session),
    )


@app.get("/signup", response_class=HTMLResponse)
def signup_page() -> RedirectResponse:
    return RedirectResponse("/login", 308)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, runner_session: str | None = Cookie(default=None)) -> HTMLResponse:
    if current_user(runner_session):
        return RedirectResponse("/", 303)
    return templates.TemplateResponse(
        request=request,
        name="auth.html",
        context=page_context(request, runner_session),
    )


@app.post("/api/auth/register/options")
def register_options(request: Request) -> JSONResponse:
    require_origin(request)
    enforce_rate(request, "register-options", limit=8, seconds=600)
    user_id = str(uuid.uuid4())
    username = f"member_{user_id.replace('-', '')[:16]}"
    display_name = "Member"
    with connection() as db:
        db.execute("DELETE FROM auth_challenges WHERE expires_at<=?", (iso(),))
        db.execute(
            """
            DELETE FROM users WHERE status='pending' AND created_at<?
            AND NOT EXISTS(SELECT 1 FROM passkeys p WHERE p.user_id=users.id)
            """,
            (iso(now() - timedelta(minutes=15)),),
        )
        db.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            (user_id, username, display_name, "pending", iso()),
        )
    options = generate_registration_options(
        rp_id=RP_ID,
        rp_name="Runner Watch",
        user_id=user_id.encode(),
        user_name=username,
        user_display_name=display_name,
        timeout=60_000,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    flow_token = save_challenge("register", options.challenge, user_id)
    return JSONResponse({"flow_token": flow_token, "options": json.loads(options_to_json(options))})


@app.post("/api/auth/register/verify")
def register_verify(payload: PasskeyFinish, request: Request) -> JSONResponse:
    require_origin(request)
    enforce_rate(request, "register-verify", limit=12, seconds=600)
    flow = take_challenge(payload.flow_token, "register")
    try:
        verification = verify_registration_response(
            credential=payload.credential,
            expected_challenge=flow["challenge"],
            expected_rp_id=RP_ID,
            expected_origin=APP_ORIGIN,
            require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(400, f"Passkey verification failed: {exc}") from exc
    response = JSONResponse({"ok": True, "redirect": "/"})
    transports = payload.credential.get("response", {}).get("transports", [])
    with connection() as db:
        db.execute(
            """
            INSERT INTO passkeys(
                credential_id,user_id,public_key,sign_count,device_type,backed_up,
                transports,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                verification.credential_id,
                flow["user_id"],
                verification.credential_public_key,
                verification.sign_count,
                enum_value(verification.credential_device_type),
                int(verification.credential_backed_up),
                json.dumps(transports),
                iso(),
            ),
        )
        db.execute("UPDATE users SET status='active' WHERE id=?", (flow["user_id"],))
    create_session(flow["user_id"], response)
    return response


@app.post("/api/auth/login/options")
def login_options(request: Request) -> JSONResponse:
    require_origin(request)
    enforce_rate(request, "login-options", limit=15, seconds=600)
    options = generate_authentication_options(
        rp_id=RP_ID,
        timeout=60_000,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    flow_token = save_challenge("login", options.challenge)
    return JSONResponse({"flow_token": flow_token, "options": json.loads(options_to_json(options))})


@app.post("/api/auth/login/verify")
def login_verify(payload: PasskeyFinish, request: Request) -> JSONResponse:
    require_origin(request)
    enforce_rate(request, "login-verify", limit=20, seconds=600)
    flow = take_challenge(payload.flow_token, "login")
    credential_id = base64url_to_bytes(payload.credential.get("id", ""))
    with connection() as db:
        passkey = db.execute(
            "SELECT * FROM passkeys WHERE credential_id=?", (credential_id,)
        ).fetchone()
    if not passkey:
        raise HTTPException(404, "This passkey is not registered here.")
    try:
        verification = verify_authentication_response(
            credential=payload.credential,
            expected_challenge=flow["challenge"],
            expected_rp_id=RP_ID,
            expected_origin=APP_ORIGIN,
            credential_public_key=passkey["public_key"],
            credential_current_sign_count=passkey["sign_count"],
            require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(400, f"Passkey login failed: {exc}") from exc
    with connection() as db:
        db.execute(
            "UPDATE passkeys SET sign_count=?,last_used_at=? WHERE credential_id=?",
            (verification.new_sign_count, iso(), credential_id),
        )
    response = JSONResponse({"ok": True, "redirect": "/"})
    create_session(passkey["user_id"], response)
    return response


@app.get("/settings/passkey", response_class=HTMLResponse)
def add_passkey_page(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    user = current_user(runner_session)
    if not user:
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(
        request=request,
        name="passkey_add.html",
        context=page_context(request, runner_session),
    )


@app.post("/api/auth/passkey/options")
def add_passkey_options(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    user = require_user(runner_session)
    enforce_rate(request, "add-passkey", limit=6, seconds=600, subject=user["id"])
    options = generate_registration_options(
        rp_id=RP_ID,
        rp_name="Runner Watch",
        user_id=user["id"].encode(),
        user_name=user["username"],
        user_display_name=user["display_name"],
        timeout=60_000,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    flow_token = save_challenge("add_passkey", options.challenge, user["id"])
    return JSONResponse({"flow_token": flow_token, "options": json.loads(options_to_json(options))})


@app.post("/api/auth/passkey/verify")
def add_passkey_verify(
    payload: PasskeyFinish,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    user = require_user(runner_session)
    enforce_rate(request, "add-passkey-verify", limit=8, seconds=600, subject=user["id"])
    flow = take_challenge(payload.flow_token, "add_passkey")
    if flow["user_id"] != user["id"]:
        raise HTTPException(403, "Passkey request does not match this account.")
    try:
        verification = verify_registration_response(
            credential=payload.credential,
            expected_challenge=flow["challenge"],
            expected_rp_id=RP_ID,
            expected_origin=APP_ORIGIN,
            require_user_verification=True,
        )
    except Exception as exc:
        raise HTTPException(400, f"Passkey verification failed: {exc}") from exc
    transports = payload.credential.get("response", {}).get("transports", [])
    with connection() as db:
        db.execute(
            """
            INSERT INTO passkeys(
                credential_id,user_id,public_key,sign_count,device_type,backed_up,
                transports,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                verification.credential_id,
                user["id"],
                verification.credential_public_key,
                verification.sign_count,
                enum_value(verification.credential_device_type),
                int(verification.credential_backed_up),
                json.dumps(transports),
                iso(),
            ),
        )
    return JSONResponse({"ok": True, "redirect": "/"})


@app.post("/api/auth/logout")
def logout(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    enforce_rate(request, "logout", limit=20, seconds=60)
    if runner_session:
        with connection() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(runner_session),))
    response = JSONResponse({"ok": True, "redirect": "/"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/scanner", response_class=HTMLResponse)
def scanner_page(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    user = current_user(runner_session)
    if not user:
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(
        request=request,
        name="scanner.html",
        context={"request": request, "user": user, "app_origin": APP_ORIGIN},
    )


def recent_sec_catalysts(tickers: list[str]) -> dict[str, dict[str, Any]]:
    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    placeholders = ",".join("?" for _ in unique)
    with connection() as db:
        rows = db.execute(
            f"""
            SELECT ticker,kind,form,filing_url,filed_at,sentiment,score,
                   beneficial_ownership_pct FROM sec_filings
            WHERE ticker IN ({placeholders}) AND created_at>?
            ORDER BY filed_at DESC
            """,  # noqa: S608
            (*unique, iso(now() - timedelta(days=3))),
        ).fetchall()
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        output.setdefault(row["ticker"], dict(row))
    return output


def recent_sec_risks(tickers: list[str]) -> dict[str, dict[str, Any]]:
    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    placeholders = ",".join("?" for _ in unique)
    with connection() as db:
        rows = db.execute(
            f"""
            SELECT ticker,kind,form,filing_url,filed_at,sentiment,score,
                   beneficial_ownership_pct
            FROM sec_filings
            WHERE ticker IN ({placeholders}) AND sentiment='risk' AND created_at>?
            ORDER BY score DESC,filed_at DESC
            """,  # noqa: S608
            (*unique, iso(now() - timedelta(days=180))),
        ).fetchall()
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        output.setdefault(str(row["ticker"]), dict(row))
    return output


def _stored_market_risk_contexts(database: Any, tickers: list[str]) -> dict[str, dict[str, Any]]:
    unique = list(dict.fromkeys(tickers))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    rows = database.execute(
        f"""
        SELECT ticker,event_type,status,event_at,last_collected_at,payload_json
        FROM market_events
        WHERE ticker IN ({placeholders}) AND event_at>?
              AND event_type IN (
                  'trading_halt','reverse_split','corporate_action','security_action'
              )
        ORDER BY event_at DESC,last_collected_at DESC
        """,  # noqa: S608
        (*unique, iso(now() - timedelta(days=370))),
    ).fetchall()
    output = {ticker: {"active_halt": False, "reverse_split_count_1y": 0} for ticker in unique}
    seen_splits: set[tuple[str, str]] = set()
    seen_halts: set[str] = set()
    checked_at = now()
    for raw in rows:
        row = dict(raw)
        ticker = str(row["ticker"])
        payload = _event_payload(row)
        event_type = str(row.get("event_type") or "").lower()
        status = str(row.get("status") or "").lower()
        if event_type == "trading_halt" and ticker not in seen_halts:
            seen_halts.add(ticker)
            last_seen = _event_timestamp(
                {"event_at": row.get("last_collected_at") or row.get("event_at")}
            )
            resume_at = _event_timestamp({"event_at": payload.get("trade_resume_at")})
            recently_confirmed = bool(last_seen and last_seen >= checked_at - timedelta(hours=24))
            output[ticker]["active_halt"] = bool(
                status in {"active", "halted", "pending"}
                and (recently_confirmed or (resume_at and resume_at > checked_at))
            )
        action = str(
            payload.get("action_type") or payload.get("type") or payload.get("description") or ""
        ).lower()
        if "reverse split" in action or event_type == "reverse_split":
            identity = (ticker, str(row.get("event_at") or "")[:10])
            if identity not in seen_splits:
                seen_splits.add(identity)
                output[ticker]["reverse_split_count_1y"] += 1
    return output


def _previous_trade_states(database: Any, tickers: list[str]) -> dict[str, str]:
    unique = list(dict.fromkeys(tickers))
    if not unique:
        return {}
    requested_rows = ",".join("(?)" for _ in unique)
    rows = database.execute(
        f"""
        WITH requested(ticker) AS (VALUES {requested_rows})
        SELECT requested.ticker,
               (
                   SELECT latest.trade_state
                   FROM scan_snapshots latest
                   WHERE latest.ticker=requested.ticker
                         AND latest.trade_state IS NOT NULL
                   ORDER BY latest.captured_at DESC
                   LIMIT 1
               ) AS trade_state
        FROM requested
        """,  # noqa: S608
        unique,
    ).fetchall()
    return {
        str(row["ticker"]): str(row["trade_state"])
        for row in rows
        if row["trade_state"] is not None
    }


def run_scan(mode: str = "penny") -> dict[str, Any]:
    with SCAN_LOCK:
        return _run_scan(mode)


def _run_scan(mode: str = "penny") -> dict[str, Any]:
    config = SCAN_MODES.get(mode)
    if not config:
        raise ValueError("Unknown scan mode")
    cached = SCAN_CACHE.get(mode)
    if cached and cached[0] > now() - timedelta(seconds=90):
        return {
            "rows": cached[1],
            "scan_run_id": cached[1][0].get("scan_run_id") if cached[1] else None,
            "mode": mode,
            "label": config["label"],
            "cached": True,
            "candidates": None,
            "eligible": None,
            "scanned": None,
            "warnings": [],
        }

    entries, universe_warnings = penny_runner_universe(
        min_price=config["min_price"],
        max_price=config["max_price"],
        fetch_recorder=record_source_fetch,
    )
    symbols = [entry.symbol for entry in entries]
    result = RunnerScanner(recording_market_data(batch_size=60)).scan(
        symbols,
        ScanSettings(
            min_price=config["min_price"],
            max_price=config["max_price"],
            min_avg_volume=100_000,
            min_avg_dollar_volume=250_000,
            max_symbols=240,
            top_n=40,
            crash_only=bool(config.get("crash_only")),
        ),
    )
    captured_at = iso()
    scan_run_id = secrets.token_urlsafe(12)
    output: list[dict[str, Any]] = []
    all_rows = result.all_rows or result.rows
    catalysts = recent_sec_catalysts([item.ticker for item in all_rows])
    persistent_risks = recent_sec_risks([item.ticker for item in all_rows])
    with connection() as db:
        tickers = [item.ticker for item in all_rows]
        issuer_context = issuer_risk_contexts(db, tickers)
        market_risk_context = _stored_market_risk_contexts(db, tickers)
        previous_states = _previous_trade_states(db, tickers)
        db.execute(
            """
            INSERT INTO scan_runs(
                id,mode,label,feature_schema_version,requested_symbols,liquid_symbols,
                scanned_symbols,candidate_rows,failed_symbols_json,warnings_json,
                started_at,finished_at,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                scan_run_id,
                mode,
                config["label"],
                FEATURE_SCHEMA_VERSION,
                result.requested_symbols,
                result.liquid_symbols,
                result.scanned_symbols,
                len(all_rows),
                json.dumps(result.failed_symbols),
                json.dumps(universe_warnings + result.warnings),
                iso(result.started_at),
                iso(result.finished_at),
                captured_at,
            ),
        )
        for baseline_rank, item in enumerate(all_rows, start=1):
            snapshot_id = secrets.token_urlsafe(10)
            catalyst = catalysts.get(item.ticker)
            risk_filing = persistent_risks.get(item.ticker)
            issuer = {
                **issuer_context.get(
                    item.ticker,
                    {"issuer_data_available": False},
                ),
                "active_risk_filing": risk_filing,
            }
            market_risk = market_risk_context.get(item.ticker, {})
            risk = assess_risk(
                RiskInput(
                    setup_score=item.score,
                    price=item.price,
                    change_pct=item.change_pct,
                    momentum_5m_pct=item.momentum_5m_pct,
                    momentum_15m_pct=item.momentum_15m_pct,
                    vwap_position_pct=item.vwap_position_pct,
                    pullback_from_high_pct=item.pullback_from_high_pct,
                    close_location=item.close_location,
                    dollar_volume=item.dollar_volume,
                    recent_dollar_volume=item.recent_dollar_volume,
                    stale_minutes=item.stale_minutes,
                    drawdown_20d_pct=item.drawdown_20d_pct,
                    drawdown_90d_pct=item.drawdown_90d_pct,
                    drawdown_52w_pct=item.drawdown_52w_pct,
                    rebound_from_20d_low_pct=item.rebound_from_20d_low_pct,
                    filing_form=risk_filing.get("form") if risk_filing else None,
                    filing_sentiment=(risk_filing.get("sentiment") if risk_filing else None),
                    filing_kind=risk_filing.get("kind") if risk_filing else None,
                    active_halt=bool(market_risk.get("active_halt")),
                    reverse_split_count_1y=int(market_risk.get("reverse_split_count_1y") or 0),
                    shares_growth_pct=issuer.get("shares_growth_pct"),
                    cash_runway_months=issuer.get("cash_runway_months"),
                    current_ratio=issuer.get("current_ratio"),
                    debt_to_cash=issuer.get("debt_to_cash"),
                    issuer_data_available=bool(issuer.get("issuer_data_available")),
                    beneficial_ownership_pct=(
                        catalyst.get("beneficial_ownership_pct") if catalyst else None
                    ),
                    previous_trade_state=previous_states.get(item.ticker),
                )
            )
            risk_factors = list(dict.fromkeys([*item.risks, *risk.risk_reasons]))
            values = {
                "id": snapshot_id,
                "scan_run_id": scan_run_id,
                "baseline_rank": baseline_rank,
                "ticker": item.ticker,
                "score": item.score,
                "setup_score": item.score,
                "rug_score": risk.rug_score,
                "rug_level": risk.rug_level,
                "trade_state": risk.trade_state,
                "state_reason": risk.state_reason,
                "hard_veto": int(risk.hard_veto),
                "crash_candidate": int(risk.crash_candidate),
                "drawdown_20d_pct": item.drawdown_20d_pct,
                "drawdown_90d_pct": item.drawdown_90d_pct,
                "drawdown_52w_pct": item.drawdown_52w_pct,
                "rebound_from_20d_low_pct": item.rebound_from_20d_low_pct,
                "risk_factors_json": json.dumps(risk_factors),
                "issuer_risk_json": json.dumps(issuer, separators=(",", ":")),
                "stage": item.stage,
                "session": item.session,
                "price": item.price,
                "change_pct": item.change_pct,
                "momentum_5m_pct": item.momentum_5m_pct,
                "momentum_15m_pct": item.momentum_15m_pct,
                "relative_volume": item.relative_volume,
                "recent_relative_volume": item.recent_relative_volume,
                "breakout_pct": item.breakout_pct,
                "range_position": item.range_position,
                "stale_minutes": item.stale_minutes,
                "session_volume": item.session_volume,
                "dollar_volume": item.dollar_volume,
                "average_volume": item.average_volume,
                "average_dollar_volume": item.average_dollar_volume,
                "momentum_previous_5m_pct": item.momentum_previous_5m_pct,
                "momentum_acceleration_pct": item.momentum_acceleration_pct,
                "intraday_volatility_pct": item.intraday_volatility_pct,
                "vwap_position_pct": item.vwap_position_pct,
                "pullback_from_high_pct": item.pullback_from_high_pct,
                "close_location": item.close_location,
                "recent_dollar_volume": item.recent_dollar_volume,
                "scoring_version": item.scoring_version,
                "quote_time": item.quote_time.isoformat(),
                "signals_json": json.dumps(item.signals),
                "risks_json": json.dumps(risk_factors),
                "captured_at": captured_at,
                "catalyst_kind": catalyst["kind"] if catalyst else None,
                "catalyst_form": catalyst["form"] if catalyst else None,
                "catalyst_sentiment": catalyst["sentiment"] if catalyst else None,
                "catalyst_score": catalyst["score"] if catalyst else None,
                "catalyst_url": catalyst["filing_url"] if catalyst else None,
                "catalyst_filed_at": catalyst["filed_at"] if catalyst else None,
                "catalyst_status": "matched_sec" if catalyst else "no_recent_sec",
            }
            db.execute(
                """
                INSERT INTO scan_snapshots(
                    id,ticker,score,stage,session,price,change_pct,
                    momentum_5m_pct,momentum_15m_pct,relative_volume,
                    recent_relative_volume,breakout_pct,dollar_volume,quote_time,
                    signals_json,risks_json,captured_at,scan_run_id,baseline_rank,
                    range_position,stale_minutes,session_volume,average_volume,
                    average_dollar_volume,catalyst_kind,catalyst_form,
                    catalyst_sentiment,catalyst_score,catalyst_filed_at,
                    momentum_previous_5m_pct,momentum_acceleration_pct,
                    intraday_volatility_pct,vwap_position_pct,
                    pullback_from_high_pct,close_location,recent_dollar_volume,
                    scoring_version,setup_score,rug_score,rug_level,trade_state,
                    state_reason,hard_veto,crash_candidate,drawdown_20d_pct,
                    drawdown_90d_pct,drawdown_52w_pct,rebound_from_20d_low_pct,
                    risk_factors_json,issuer_risk_json
                ) VALUES(
                    :id,:ticker,:score,:stage,:session,:price,:change_pct,
                    :momentum_5m_pct,:momentum_15m_pct,:relative_volume,
                    :recent_relative_volume,:breakout_pct,:dollar_volume,:quote_time,
                    :signals_json,:risks_json,:captured_at,:scan_run_id,:baseline_rank,
                    :range_position,:stale_minutes,:session_volume,:average_volume,
                    :average_dollar_volume,:catalyst_kind,:catalyst_form,
                    :catalyst_sentiment,:catalyst_score,:catalyst_filed_at,
                    :momentum_previous_5m_pct,:momentum_acceleration_pct,
                    :intraday_volatility_pct,:vwap_position_pct,
                    :pullback_from_high_pct,:close_location,:recent_dollar_volume,
                    :scoring_version,:setup_score,:rug_score,:rug_level,:trade_state,
                    :state_reason,:hard_veto,:crash_candidate,:drawdown_20d_pct,
                    :drawdown_90d_pct,:drawdown_52w_pct,:rebound_from_20d_low_pct,
                    :risk_factors_json,:issuer_risk_json
                )
                """,
                values,
            )
            if baseline_rank <= len(result.rows):
                output.append(values)

    prediction = predict_and_store(scan_run_id)
    kol_result: dict[str, Any] = {
        "calls_created": 0,
        "calls_abandoned": 0,
    }
    if prediction.get("predicted"):
        with connection() as db:
            predicted_rows = db.execute(
                """
                SELECT snapshot_id,score,rank,probability_up,probability_down,
                       probability_timeout,expected_return_pct
                FROM ranker_predictions
                WHERE model_id=? AND snapshot_id IN (
                    SELECT id FROM scan_snapshots WHERE scan_run_id=?
                )
                """,
                (prediction["model_id"], scan_run_id),
            ).fetchall()
        predicted = {row["snapshot_id"]: dict(row) for row in predicted_rows}
        for values in output:
            row = predicted.get(values["id"])
            if row:
                values["runner_probability"] = row["probability_up"]
                values["custom_score"] = row["score"]
                values["custom_rank"] = row["rank"]
                values["expected_return_pct"] = row["expected_return_pct"]
                values["ranker_model_id"] = prediction["model_id"]
        kol_result = publish_calls_for_scan(scan_run_id, str(prediction["model_id"]))
        calls_by_ticker = calls_for_tickers([str(values["ticker"]) for values in output])
        for values in output:
            values["kol_calls"] = calls_by_ticker.get(str(values["ticker"]), [])
    SCAN_CACHE[mode] = (now(), output)
    return {
        "rows": output,
        "scan_run_id": scan_run_id,
        "mode": mode,
        "label": config["label"],
        "cached": False,
        "candidates": len(symbols),
        "eligible": result.liquid_symbols,
        "scanned": result.scanned_symbols,
        "ranked_candidates": len(all_rows),
        "elapsed_seconds": round(result.elapsed_seconds, 1),
        "kol": kol_result,
        "warnings": (universe_warnings + result.warnings)[:4],
    }


@app.post("/api/scan")
async def api_scan(
    request: Request,
    mode: str = "penny",
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    user = require_user(runner_session)
    enforce_rate(request, "scan", limit=6, seconds=600, subject=user["id"])
    if mode not in SCAN_MODES:
        raise HTTPException(400, "Unknown scan mode")
    result = await run_in_threadpool(run_scan, mode)
    return JSONResponse(result)


@app.post("/api/signals")
def publish_signal(
    payload: PublishSignal,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    user = require_user(runner_session)
    enforce_rate(request, "publish", limit=8, seconds=3600, subject=user["id"])
    with connection() as db:
        recent_count = db.execute(
            "SELECT COUNT(*) FROM signals WHERE user_id=? AND created_at>?",
            (user["id"], iso(now() - timedelta(hours=1))),
        ).fetchone()[0]
        if recent_count >= 8:
            raise HTTPException(429, "Publishing limit reached. Try again later.")
        snapshot = db.execute(
            "SELECT * FROM scan_snapshots WHERE id=? AND captured_at>?",
            (payload.snapshot_id, iso(now() - timedelta(hours=2))),
        ).fetchone()
        if not snapshot:
            raise HTTPException(400, "That scan is too old. Run a fresh scan.")
        if bool(snapshot["hard_veto"]) or str(snapshot["trade_state"] or "").upper() in {
            "AVOID",
            "EXIT",
        }:
            raise HTTPException(
                400,
                "Rug risk blocks publishing this row as alpha. Share the risk evidence instead.",
            )
        signal_id = str(uuid.uuid4())
        public_id = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        db.execute(
            """
            INSERT INTO signals(
                id,public_id,snapshot_id,user_id,thesis,horizon,invalidation,
                disclosure,status,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                signal_id,
                public_id,
                payload.snapshot_id,
                user["id"],
                payload.thesis.strip(),
                payload.horizon,
                payload.invalidation.strip(),
                payload.disclosure.strip(),
                "public",
                iso(),
            ),
        )
    return JSONResponse({"ok": True, "url": f"/s/{public_id}"})


def get_signal(public_id: str) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            """
            SELECT sig.id AS signal_id,sig.public_id,sig.thesis,sig.horizon,
                   sig.invalidation,sig.disclosure,sig.created_at,u.username,
                   u.display_name,s.*,o.barrier_label,o.return_60m_pct,
                   o.max_favorable_pct,o.max_adverse_pct
            FROM signals sig JOIN users u ON u.id=sig.user_id
            JOIN scan_snapshots s ON s.id=sig.snapshot_id
            LEFT JOIN scan_outcomes o ON o.snapshot_id=s.id
            WHERE sig.public_id=? AND sig.status='public'
            """,
            (public_id,),
        ).fetchone()
    signal = row_dict(row)
    if signal:
        signal["coin_tone"] = _coin_tone(signal["ticker"])
    return signal


@app.get("/s/{public_id}", response_class=HTMLResponse)
def signal_page(
    public_id: str,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    signal = get_signal(public_id)
    if not signal:
        raise HTTPException(404, "Signal not found")
    signal["signals"] = json.loads(signal["signals_json"])
    signal["risks"] = json.loads(signal["risks_json"])
    return templates.TemplateResponse(
        request=request,
        name="signal.html",
        context=page_context(request, runner_session, signal=signal),
    )


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size)
    except OSError:
        return ImageFont.load_default()


@app.get("/s/{public_id}/card.png")
def signal_card(public_id: str) -> Response:
    signal = get_signal(public_id)
    if not signal:
        raise HTTPException(404, "Signal not found")
    image = Image.new("RGB", (1200, 630), "#07110d")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (55, 55, 1145, 575), radius=32, fill="#102019", outline="#4ade80", width=3
    )
    draw.text((95, 90), "RUNNER WATCH", fill="#86efac", font=font(34, True))
    draw.text((95, 165), f"${signal['ticker']}", fill="white", font=font(88, True))
    change = f"{signal['change_pct']:+.1f}%"
    draw.text((95, 275), change, fill="#4ade80", font=font(64, True))
    draw.text(
        (420, 185),
        (
            f"{signal.get('trade_state') or 'WATCH'}  ·  "
            f"SETUP {float(signal.get('setup_score') or signal['score']):.0f}  ·  "
            f"RUG {float(signal.get('rug_score') or 0):.0f}"
        ),
        fill="#d1fae5",
        font=font(30, True),
    )
    thesis = signal["thesis"][:100]
    if len(signal["thesis"]) > 100:
        thesis += "…"
    draw.multiline_text((420, 255), thesis, fill="#ecfdf5", font=font(30), spacing=12)
    draw.text(
        (95, 505),
        f"@{signal['username']}  ·  delayed market data",
        fill="#9ca3af",
        font=font(25),
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return Response(
        buffer.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public,max-age=300"},
    )


@app.post("/api/signals/{public_id}/report")
def report_signal(
    public_id: str,
    payload: ReportSignal,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    user = require_user(runner_session)
    enforce_rate(request, "report", limit=20, seconds=3600, subject=user["id"])
    signal = get_signal(public_id)
    if not signal:
        raise HTTPException(404, "Signal not found")
    with connection() as db:
        db.execute(
            """
            INSERT OR IGNORE INTO reports(id,signal_id,user_id,reason,created_at)
            VALUES(?,?,?,?,?)
            """,
            (str(uuid.uuid4()), signal["signal_id"], user["id"], payload.reason.strip(), iso()),
        )
        reports = db.execute(
            "SELECT COUNT(*) FROM reports WHERE signal_id=?", (signal["signal_id"],)
        ).fetchone()[0]
        under_review = reports >= 3
        if under_review:
            db.execute("UPDATE signals SET status='review' WHERE id=?", (signal["signal_id"],))
    return JSONResponse({"ok": True, "under_review": under_review})
