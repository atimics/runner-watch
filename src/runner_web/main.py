from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import re
import secrets
import sqlite3
import textwrap
import threading
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any
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
from runner_watch.scanner import RunnerScanner
from runner_watch.universe import penny_runner_universe
from runner_web.collection import recording_market_data
from runner_web.db import connection, init_db
from runner_web.ingestion import ingestion_status, record_source_fetch
from runner_web.intelligence import record_edgar_error, refresh_edgar
from runner_web.market_clock import market_clock
from runner_web.outcomes import record_outcome_error, refresh_outcomes, refresh_scan_outcomes
from runner_web.ranker import (
    FEATURE_SCHEMA_VERSION,
    predict_and_store,
    ranker_status,
    train_shadow_ranker,
)
from runner_web.source_workers import trading_halt_worker

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
OPENROUTER_RESEARCH_MODEL = os.getenv("OPENROUTER_RESEARCH_MODEL", "z-ai/glm-5.3")
SCAN_MODES = {
    "penny": {"label": "Penny stocks", "min_price": 0.20, "max_price": 5.00},
    "low_price": {"label": "Low-priced small caps", "min_price": 0.20, "max_price": 20.00},
}
SCAN_CACHE: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
CHART_CACHE: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
SCAN_LOCK = threading.Lock()
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMITS: dict[str, list[datetime]] = {}
EASTERN = ZoneInfo("America/New_York")
BACKGROUND_SCAN_INTERVAL_SECONDS = max(
    120, int(os.getenv("BACKGROUND_SCAN_INTERVAL_SECONDS", "180"))
)


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_db()
    tasks = [
        asyncio.create_task(edgar_worker()),
        asyncio.create_task(trading_halt_worker()),
        asyncio.create_task(outcome_worker()),
        asyncio.create_task(scan_collection_worker()),
        asyncio.create_task(alpha_report_worker()),
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
                name
                for name, stamps in RATE_LIMITS.items()
                if not stamps or stamps[-1] <= cutoff
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
            if scan_result["barrier_labels_added"]:
                await run_in_threadpool(train_shadow_ranker)
            await run_in_threadpool(prune_storage)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record_outcome_error(exc)
        await asyncio.sleep(600)


async def scan_collection_worker() -> None:
    await asyncio.sleep(90)
    while True:
        eastern_now = now().astimezone(EASTERN)
        market_window = clock_time(4) <= eastern_now.time().replace(tzinfo=None) < clock_time(20)
        if eastern_now.weekday() < 5 and market_window:
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


@app.get("/api/ingestion/status")
def api_ingestion_status() -> dict[str, Any]:
    return ingestion_status()


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
        row["evidence_label"] = "Verified insider purchase"
        if shares and price:
            row["evidence_text"] = (
                f"{actor} reported buying {float(shares):,.0f} shares at "
                f"${float(price):,.2f}."
            )
        else:
            row["evidence_text"] = "The structured Form 4 contains transaction code P."
    elif "S" in codes:
        row["evidence_label"] = "Verified insider sale"
        row["evidence_text"] = (
            f"{actor} reported a sale. Check the filing for plan and ownership context."
        )
    elif row.get("sentiment") == "risk":
        row["evidence_label"] = "Direct SEC risk filing"
        row["evidence_text"] = "The form type can signal supply, dilution, or reporting risk."
    elif str(row.get("form", "")).startswith("4"):
        row["evidence_label"] = "Structured ownership filing"
        row["evidence_text"] = "This is an ownership change, but not a code P purchase."
    else:
        row["evidence_label"] = "New primary-source filing"
        row["evidence_text"] = "The event is new and still needs human review of the filing."
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
            "note": "Collecting enough 5-minute bars to estimate trade pressure.",
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
        "note": "Estimate from 5-minute OHLCV bars, not bids, asks, trades, or Level II depth.",
    }


def _evidence_gate(
    current: dict[str, Any],
    events: list[dict[str, Any]],
    pressure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[str] = []
    relative_volume = _number(current.get("relative_volume"))
    recent_relative_volume = _number(current.get("recent_relative_volume"))
    momentum_15m = _number(current.get("momentum_15m_pct"))
    acceleration = _number(current.get("momentum_acceleration_pct"))
    vwap_position = _number(current.get("vwap_position_pct"))
    breakout = _number(current.get("breakout_pct"))
    if relative_volume is not None and relative_volume >= 2:
        checks.append("Unusual session volume")
    if recent_relative_volume is not None and recent_relative_volume >= 3:
        checks.append("Fresh volume burst")
    if momentum_15m is not None and momentum_15m >= 3:
        checks.append("15-minute momentum")
    if acceleration is not None and acceleration >= 0.5:
        checks.append("Momentum accelerating")
    if vwap_position is not None and vwap_position > 0:
        checks.append("Holding above VWAP")
    if breakout is not None and breakout > 0:
        checks.append("Breaking prior high")
    if current.get("catalyst_sentiment") == "positive" or any(
        event.get("sentiment") == "positive" for event in events
    ):
        checks.append("Positive SEC catalyst")
    if pressure and pressure.get("available") and pressure.get("buy_pressure_pct", 0) >= 60:
        checks.append("Bar-derived buy pressure")
    threshold = 4
    count = len(checks)
    state = "ready" if count >= threshold else "near" if count == threshold - 1 else "gathering"
    return {
        "count": count,
        "threshold": threshold,
        "state": state,
        "checks": checks,
        "summary": (
            "Research-ready threshold reached"
            if state == "ready"
            else "One more check to research-ready"
            if state == "near"
            else "Gathering deterministic evidence"
        ),
    }


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


def pulse_data(*, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    offset = max(0, offset)
    limit = max(1, min(limit, 50))
    cutoff = iso(now() - timedelta(days=3))
    market_cutoff = iso(now() - timedelta(minutes=12))
    with connection() as db:
        filing_rows = db.execute(
            """
            SELECT f.*,o.return_1h_pct,o.return_1d_pct,o.return_5d_pct
            FROM sec_filings f
            LEFT JOIN sec_outcomes o ON o.accession=f.accession
            WHERE f.created_at>?
            ORDER BY f.score DESC,f.filed_at DESC
            """,
            (cutoff,),
        ).fetchall()
        market_rows = db.execute(
            """
            SELECT s.*,
                   (SELECT c.name FROM sec_companies c
                    WHERE c.ticker=s.ticker LIMIT 1) AS listed_company
            FROM scan_snapshots s
            JOIN (
                SELECT ticker,MAX(captured_at) AS captured_at
                FROM scan_snapshots WHERE captured_at>? GROUP BY ticker
            ) latest ON latest.ticker=s.ticker AND latest.captured_at=s.captured_at
            ORDER BY s.score DESC LIMIT 40
            """,
            (market_cutoff,),
        ).fetchall()
        state_rows = db.execute(
            """
            SELECT key,value,updated_at FROM worker_state
            WHERE key LIKE 'edgar_%' OR key LIKE 'background_scan_%'
            """
        ).fetchall()

    filings_by_ticker: dict[str, dict[str, Any]] = {}
    filing_counts: dict[str, int] = {}
    penny_filings = 0
    for raw in filing_rows:
        event = _intelligence_evidence(dict(raw))
        if event.get("price") is None or not 0.20 <= float(event["price"]) <= 5:
            continue
        penny_filings += 1
        ticker = event["ticker"]
        filing_counts[ticker] = filing_counts.get(ticker, 0) + 1
        filings_by_ticker.setdefault(ticker, event)

    runner_rows: list[dict[str, Any]] = []
    runner_tickers: set[str] = set()
    unexplained = 0
    for raw in market_rows:
        snapshot = dict(raw)
        if not 0.20 <= float(snapshot["price"]) <= 5:
            continue
        ticker = snapshot["ticker"]
        relative_volume = snapshot.get("relative_volume")
        if abs(float(snapshot["change_pct"])) < 5 and (
            relative_volume is None or float(relative_volume) < 2
        ):
            continue
        catalyst = filings_by_ticker.get(ticker)
        if not catalyst:
            unexplained += 1
        boost = 5.0 if catalyst and catalyst.get("sentiment") == "positive" else 0.0
        runner_tickers.add(ticker)
        runner = {
            **snapshot,
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
            "pulse_label": _market_pulse_label(snapshot, catalyst),
            "event_count": filing_counts.get(ticker, 0),
            "source": "market",
            "section": "runners",
            "event_at": snapshot["captured_at"],
            "attention_score": float(snapshot.get("score") or 0) + boost,
            "filing_url": catalyst.get("filing_url") if catalyst else None,
            "signals": _json_list(snapshot.get("signals_json")),
            "risks": _json_list(snapshot.get("risks_json")),
        }
        runner["evidence_gate"] = _evidence_gate(
            runner, [catalyst] if catalyst else []
        )
        runner_rows.append(runner)

    runner_rows.sort(
        key=lambda row: (float(row["attention_score"]), str(row["event_at"])),
        reverse=True,
    )
    event_rows: list[dict[str, Any]] = []
    for ticker, event in filings_by_ticker.items():
        if ticker in runner_tickers:
            continue
        filing_row = {
            **event,
            "coin_label": ticker[:2],
            "coin_tone": _coin_tone(ticker),
            "pulse_label": _pulse_label(event),
            "event_count": filing_counts[ticker],
            "source": "sec",
            "section": "filings",
            "event_at": event["filed_at"],
            "attention_score": float(event.get("score") or 0),
        }
        filing_row["evidence_gate"] = _evidence_gate(filing_row, [event])
        event_rows.append(filing_row)
    event_rows.sort(key=lambda row: str(row["event_at"]), reverse=True)
    all_rows = runner_rows + event_rows
    rows = all_rows[offset : offset + limit]
    state = {row["key"]: row["value"] for row in state_rows}
    market_updated_at = max(
        (str(row["captured_at"]) for row in market_rows),
        default=None,
    )
    updated_at = max(
        [stamp for stamp in (market_updated_at, state.get("edgar_last_refresh")) if stamp],
        default=None,
    )
    return {
        "rows": rows,
        "stats": {
            "live": len(rows),
            "runners": len(runner_rows),
            "unexplained": unexplained,
            "filings": penny_filings,
        },
        "updated_at": updated_at,
        "market_updated_at": market_updated_at,
        "next_offset": offset + len(rows),
        "has_more": offset + len(rows) < len(all_rows),
    }


def _report_record(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    report = dict(row)
    for key in ("catalysts_json", "risks_json", "watch_json", "sources_json"):
        report[key.removesuffix("_json")] = _json_list(report.get(key))
    return report


def _commission_record(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    report = dict(row)
    for key in ("catalysts_json", "risks_json", "watch_json", "sources_json"):
        report[key.removesuffix("_json")] = _json_list(report.get(key))
    try:
        report["usage"] = json.loads(report.get("usage_json") or "{}")
    except (TypeError, ValueError):
        report["usage"] = {}
    summary = _ticker_summary(report["ticker"])
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


def alpha_board_data(profile: str) -> dict[str, Any]:
    with connection() as db:
        heart_rows = db.execute(
            """
            SELECT ticker,COUNT(*) AS heart_count,MAX(updated_at) AS latest_heart
            FROM ticker_hearts WHERE active=1
            GROUP BY ticker ORDER BY heart_count DESC,latest_heart DESC LIMIT 50
            """
        ).fetchall()
        mine = {
            row["ticker"]
            for row in db.execute(
                "SELECT ticker FROM ticker_hearts WHERE profile_id=? AND active=1",
                (profile,),
            ).fetchall()
        }
    rows: list[dict[str, Any]] = []
    for rank, heart_row in enumerate(heart_rows, start=1):
        ticker = heart_row["ticker"]
        item = _ticker_summary(ticker)
        if not item:
            continue
        with connection() as db:
            report_row = db.execute(
                """
                SELECT * FROM alpha_reports
                WHERE ticker=? AND status='complete' ORDER BY created_at DESC LIMIT 1
                """,
                (ticker,),
            ).fetchone()
        item.update(
            rank=rank,
            heart_count=int(heart_row["heart_count"]),
            latest_heart=heart_row["latest_heart"],
            hearted=ticker in mine,
            is_leader=rank == 1,
            ai_report=_report_record(report_row),
        )
        rows.append(item)
    ranked = {row["ticker"] for row in rows}
    contenders = [row for row in pulse_data(limit=8)["rows"] if row["ticker"] not in ranked][:5]
    return {
        "rows": rows,
        "contenders": contenders,
        "total_hearts": sum(row["heart_count"] for row in rows),
        "provider_ready": bool(AI_REPORT_API_KEY),
        "commissions": commissioned_reports(),
    }


def _alpha_evidence(ticker: str, heart_count: int) -> tuple[str, dict[str, Any]]:
    detail = ticker_detail_data(ticker)
    if not detail:
        raise ValueError("Ticker detail is unavailable")
    current = detail["current"]
    filings = [
        {
            "form": event.get("form"),
            "filed_at": event.get("filed_at"),
            "label": event.get("evidence_label"),
            "text": event.get("evidence_text"),
            "url": event.get("filing_url"),
        }
        for event in detail["events"][:5]
    ]
    evidence = {
        "ticker": ticker,
        "company": detail["company"],
        "heart_count": heart_count,
        "captured_at": current.get("event_at"),
        "price": current.get("price"),
        "change_pct": current.get("change_pct"),
        "score": current.get("score"),
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
            "ticker": ticker,
            "event_at": current.get("event_at"),
            "filings": [item.get("filed_at") for item in filings],
            "checks": detail["evidence_gate"]["checks"],
        },
        sort_keys=True,
    )
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:24], evidence


def _generate_openrouter_report(
    openrouter_key: str,
    evidence: dict[str, Any],
    user_id: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    required = ("headline", "summary", "catalysts", "risks", "watch")
    body = {
        "model": OPENROUTER_RESEARCH_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Return JSON using only the evidence. Keys: headline and summary strings; "
                    "catalysts, risks, and watch string arrays. No outside facts or advice."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(evidence, separators=(",", ":")),
            },
        ],
        "response_format": {"type": "json_object"},
        "provider": {"require_parameters": True},
        "reasoning_effort": "low",
        "max_tokens": 1600,
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
        with urllib.request.urlopen(api_request, timeout=75) as response:  # noqa: S310
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
        raise HTTPException(exc.code if exc.code < 500 else 502, message) from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise HTTPException(504, "OpenRouter took too long to answer.") from exc
    try:
        content = result["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(str(item.get("text") or "") for item in content)
        report = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise HTTPException(502, "OpenRouter returned an incomplete report.") from exc
    if (
        not isinstance(report, dict)
        or not all(key in report for key in required)
        or not all(isinstance(report[key], str) for key in ("headline", "summary"))
        or not all(
            isinstance(report[key], list) for key in ("catalysts", "risks", "watch")
        )
    ):
        raise HTTPException(502, "OpenRouter returned an incomplete report.")
    return report, str(result.get("model") or OPENROUTER_RESEARCH_MODEL), dict(
        result.get("usage") or {}
    )


def _commission_research(
    user_id: str,
    ticker: str,
    openrouter_key: str,
) -> dict[str, Any]:
    timestamp = iso()
    with connection() as db:
        recent_count = db.execute(
            """
            SELECT COUNT(*) FROM research_commissions
            WHERE user_id=? AND status IN ('running','complete') AND created_at>?
            """,
            (user_id, iso(now() - timedelta(days=1))),
        ).fetchone()[0]
        if recent_count >= 3:
            raise HTTPException(429, "You can commission three reports per day.")
        running = db.execute(
            """
            SELECT public_id FROM research_commissions
            WHERE user_id=? AND ticker=? AND status='running'
            """,
            (user_id, ticker),
        ).fetchone()
        if running:
            raise HTTPException(409, "This report is already running.")
        heart_count = db.execute(
            "SELECT COUNT(*) FROM ticker_hearts WHERE ticker=? AND active=1", (ticker,)
        ).fetchone()[0]
    evidence_key, evidence = _alpha_evidence(ticker, int(heart_count))
    report_id = str(uuid.uuid4())
    public_id = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]
    with connection() as db:
        try:
            db.execute(
                """
                INSERT INTO research_commissions(
                    id,public_id,user_id,ticker,evidence_key,status,requested_model,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,'running',?,?,?)
                """,
                (
                    report_id,
                    public_id,
                    user_id,
                    ticker,
                    evidence_key,
                    OPENROUTER_RESEARCH_MODEL,
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "This report is already running.") from exc
    try:
        report, model, usage = _generate_openrouter_report(openrouter_key, evidence, user_id)
        sources = [item["url"] for item in evidence["filings"] if item.get("url")]
        completed_at = iso()
        with connection() as db:
            db.execute(
                """
                UPDATE research_commissions SET status='complete',model=?,headline=?,summary=?,
                    catalysts_json=?,risks_json=?,watch_json=?,sources_json=?,usage_json=?,
                    error=NULL,updated_at=?,completed_at=? WHERE id=?
                """,
                (
                    model[:160],
                    str(report["headline"])[:180],
                    str(report["summary"])[:1800],
                    json.dumps(list(report["catalysts"])[:8]),
                    json.dumps(list(report["risks"])[:8]),
                    json.dumps(list(report["watch"])[:8]),
                    json.dumps(sources[:8]),
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
        return _commission_record(row) or {}
    except Exception as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else "Report generation failed."
        with connection() as db:
            db.execute(
                """
                UPDATE research_commissions SET status='failed',error=?,updated_at=? WHERE id=?
                """,
                (str(detail)[:500], iso(), report_id),
            )
        raise


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
            WHERE user_id=? AND ticker=? AND status='complete'
            ORDER BY completed_at DESC LIMIT 1
            """,
            (user_id, ticker),
        ).fetchone()
    return _commission_record(row)


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
            "Write a concise stock research report using only the supplied evidence. "
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


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="pulse.html",
        context=page_context(
            request,
            runner_session,
            pulse=pulse_data(limit=20),
            active_tab="pulse",
        ),
    )


@app.get("/api/pulse")
def pulse_api(request: Request, offset: int = 0, limit: int = 20) -> JSONResponse:
    enforce_rate(request, "pulse", limit=180, seconds=60)
    return JSONResponse(pulse_data(offset=offset, limit=limit))


@app.get("/api/pulse/charts")
async def pulse_charts_api(request: Request) -> JSONResponse:
    enforce_rate(request, "pulse-charts", limit=20, seconds=60)
    tickers = [row["ticker"] for row in pulse_data()["rows"]]
    charts = await run_in_threadpool(ticker_charts_data, tickers)
    return JSONResponse({"charts": charts})


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
                UNION SELECT 1 FROM scan_snapshots WHERE ticker=? LIMIT 1
                """,
                (ticker, ticker, ticker),
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

    events = []
    for row in filings:
        event = _intelligence_evidence(dict(row))
        event["pulse_label"] = _pulse_label(event)
        event["event_at"] = event["filed_at"]
        events.append(event)
    if not events and snapshot is None and company is None:
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
    return {
        "ticker": ticker,
        "company": company["name"] if company else current.get("company", ticker),
        "exchange": company["exchange"] if company else "Listed US stock",
        "coin_label": ticker[:2],
        "coin_tone": _coin_tone(ticker),
        "current": current,
        "events": events,
        "trade_pressure": pressure,
        "evidence_gate": _evidence_gate(current, events, pressure),
        "can_publish": bool(
            snapshot is not None
            and str(snapshot["captured_at"]) > iso(now() - timedelta(hours=2))
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


def ticker_charts_data(tickers: list[str]) -> dict[str, list[dict[str, Any]]]:
    requested = list(dict.fromkeys(tickers))[:50]
    cutoff = now() - timedelta(seconds=90)
    output = {
        ticker: cached[1]
        for ticker in requested
        if (cached := CHART_CACHE.get(ticker)) and cached[0] > cutoff
    }
    missing = [ticker for ticker in requested if ticker not in output]
    if missing:
        result = recording_market_data(batch_size=60).intraday(missing)
        for ticker in missing:
            points = _chart_points(result.frames.get(ticker))
            CHART_CACHE[ticker] = (now(), points)
            output[ticker] = points
    if len(CHART_CACHE) > 500:
        oldest = sorted(CHART_CACHE, key=lambda ticker: CHART_CACHE[ticker][0])
        for ticker in oldest[: len(CHART_CACHE) - 500]:
            CHART_CACHE.pop(ticker, None)
    return output


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
    return templates.TemplateResponse(
        request=request,
        name="ticker.html",
        context=page_context(
            request,
            runner_session,
            detail=detail,
            heart=heart_state(normalized, profile),
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
    points = await run_in_threadpool(ticker_chart_data, normalized)
    return JSONResponse({"ticker": normalized, "points": points})


@app.get("/api/t/{ticker}/pressure")
async def ticker_pressure_api(ticker: str, request: Request) -> JSONResponse:
    enforce_rate(request, "ticker-pressure", limit=60, seconds=60)
    normalized = _clean_ticker(ticker)
    if not _ticker_exists(normalized):
        raise HTTPException(404, "Ticker not found")
    await run_in_threadpool(ticker_chart_data, normalized)
    pressure = await run_in_threadpool(_market_trade_pressure, normalized)
    detail = ticker_detail_data(normalized)
    gate = _evidence_gate(detail["current"], detail["events"], pressure) if detail else None
    return JSONResponse({"ticker": normalized, "pressure": pressure, "evidence_gate": gate})


def _ticker_summary(ticker: str) -> dict[str, Any] | None:
    detail = ticker_detail_data(ticker)
    if not detail:
        return None
    current = dict(detail["current"])
    filing = detail["events"][0] if detail["events"] else None
    source = str(current.get("source") or "quiet")
    if source == "market":
        pulse_label = _market_pulse_label(current, filing)
    elif filing:
        pulse_label = _pulse_label(filing)
    else:
        pulse_label = "Quiet"
    return {
        **current,
        "ticker": ticker,
        "company": detail["company"],
        "exchange": detail["exchange"],
        "coin_label": detail["coin_label"],
        "coin_tone": detail["coin_tone"],
        "source": source,
        "pulse_label": pulse_label,
        "event_count": len(detail["events"]),
        "event_at": current.get("event_at"),
        "sentiment": filing.get("sentiment") if filing else current.get("sentiment", "gap"),
        "evidence_gate": detail["evidence_gate"],
        "filing_url": filing.get("filing_url") if filing else None,
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


def radar_data(
    user_id: str | None = None,
    *,
    visitor_id: str | None = None,
    mark_seen: bool = False,
) -> list[dict[str, Any]]:
    profile = f"u:{user_id}" if user_id else f"v:{visitor_id or 'guest'}"
    scores = _activity_scores(profile, user_id)
    pulse_rows = pulse_data(limit=50)["rows"]
    pulse_by_ticker = {row["ticker"]: row for row in pulse_rows}
    ordered = sorted(scores, key=scores.get, reverse=True)
    for row in pulse_rows:
        if row["ticker"] not in ordered:
            ordered.append(row["ticker"])
        if len(ordered) >= 20:
            break
    with connection() as db:
        seen = {
            row["ticker"]: row["last_seen_at"]
            for row in db.execute(
                "SELECT ticker,last_seen_at FROM radar_seen WHERE profile_id=?", (profile,)
            ).fetchall()
        }
    output: list[dict[str, Any]] = []
    for ticker in ordered[:20]:
        item = dict(pulse_by_ticker.get(ticker) or _ticker_summary(ticker) or {})
        if not item:
            continue
        item["relevance_score"] = round(scores.get(ticker, 0.0), 2)
        event_at = item.get("event_at")
        item["has_update"] = bool(event_at and (not seen.get(ticker) or event_at > seen[ticker]))
        output.append(item)
    output.sort(
        key=lambda row: (
            float(row.get("relevance_score") or 0),
            float(row.get("attention_score") or row.get("score") or 0),
            str(row.get("event_at") or ""),
        ),
        reverse=True,
    )
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
    charts = await run_in_threadpool(ticker_charts_data, tickers)
    return JSONResponse({"charts": charts})


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
    state = heart_state(normalized, profile)
    return JSONResponse(state)


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
    report = await run_in_threadpool(
        _commission_research,
        user["id"],
        normalized,
        openrouter_key,
    )
    return JSONResponse(
        {
            "ok": True,
            "ticker": normalized,
            "url": f"/research/{report['public_id']}",
        }
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
    image = Image.new("RGB", (1200, 630), "#090b0b")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (55, 55, 1145, 575), radius=34, fill="#111514", outline="#57e389", width=3
    )
    draw.text((95, 88), "RUNNER WATCH · COMMISSIONED RESEARCH", "#87e8a9", font=font(29, True))
    draw.text((95, 150), f"${report['ticker']}", "#f4f8f6", font=font(84, True))
    headline = "\n".join(textwrap.wrap(str(report["headline"]), width=39)[:3])
    draw.multiline_text(
        (95, 265), headline, fill="#f4f8f6", font=font(37, True), spacing=11
    )
    draw.text(
        (95, 515),
        str(report.get("model") or report["requested_model"])[:70],
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
                1
                for row in events
                if row.get("price") is not None and row["price"] <= 5
            ),
            "labeled_events": sum(
                1
                for row in events
                if any(row.get(key) is not None for key in outcome_keys)
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
def login_page(
    request: Request, runner_session: str | None = Cookie(default=None)
) -> HTMLResponse:
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
            SELECT ticker,kind,form,filing_url,filed_at,sentiment,score FROM sec_filings
            WHERE ticker IN ({placeholders}) AND created_at>?
            ORDER BY filed_at DESC
            """,  # noqa: S608
            (*unique, iso(now() - timedelta(days=3))),
        ).fetchall()
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        output.setdefault(row["ticker"], dict(row))
    return output


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
        ),
    )
    captured_at = iso()
    scan_run_id = secrets.token_urlsafe(12)
    output: list[dict[str, Any]] = []
    all_rows = result.all_rows or result.rows
    catalysts = recent_sec_catalysts([item.ticker for item in all_rows])
    with connection() as db:
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
            values = {
                "id": snapshot_id,
                "scan_run_id": scan_run_id,
                "baseline_rank": baseline_rank,
                "ticker": item.ticker,
                "score": item.score,
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
                "risks_json": json.dumps(item.risks),
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
                    scoring_version
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
                    :scoring_version
                )
                """,
                values,
            )
            if baseline_rank <= len(result.rows):
                output.append(values)

    prediction = predict_and_store(scan_run_id)
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
        f"{signal['stage']}  ·  SCORE {signal['score']:.1f}",
        fill="#d1fae5",
        font=font(38, True),
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
