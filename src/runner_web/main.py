from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
    AuthenticatorSelectionCriteria,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from runner_watch.market_data import YahooMarketData
from runner_watch.models import ScanSettings
from runner_watch.scanner import RunnerScanner
from runner_watch.universe import penny_runner_universe
from runner_web.db import connection, init_db
from runner_web.intelligence import record_edgar_error, refresh_edgar
from runner_web.outcomes import record_outcome_error, refresh_outcomes

APP_ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:8080").rstrip("/")
RP_ID = os.getenv("RP_ID", "localhost")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
ROOT = Path(os.getenv("RUNNER_ROOT", Path.cwd()))
SESSION_COOKIE = "runner_session"
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,24}$")
TICKER_RE = re.compile(r"^[A-Z0-9.-]{1,12}$")
SCAN_MODES = {
    "penny": {"label": "Penny stocks", "min_price": 0.20, "max_price": 5.00},
    "low_price": {"label": "Low-priced small caps", "min_price": 0.20, "max_price": 20.00},
}
SCAN_CACHE: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
CHART_CACHE: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}

app = FastAPI(title="Runner Watch", docs_url=None, redoc_url=None)
templates = Jinja2Templates(directory=str(ROOT / "web" / "templates"))
app.mount("/static", StaticFiles(directory=str(ROOT / "web" / "static")), name="static")


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat()


def row_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != APP_ORIGIN:
        raise HTTPException(403, "Origin check failed")


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
    return {
        "request": request,
        "user": current_user(session_token),
        "app_origin": APP_ORIGIN,
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record_outcome_error(exc)
        await asyncio.sleep(600)


@app.on_event("startup")
async def startup() -> None:
    init_db()
    app.state.edgar_task = asyncio.create_task(edgar_worker())
    app.state.outcome_task = asyncio.create_task(outcome_worker())


@app.on_event("shutdown")
async def shutdown() -> None:
    tasks = [
        task
        for task in (
            getattr(app.state, "edgar_task", None),
            getattr(app.state, "outcome_task", None),
        )
        if task
    ]
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    return response


class RegisterStart(BaseModel):
    username: str
    display_name: str = Field(min_length=2, max_length=40)


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


@app.get("/health")
def health() -> dict[str, str]:
    with connection() as db:
        db.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/community", response_class=HTMLResponse)
def community(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    with connection() as db:
        rows = db.execute(
            """
            SELECT sig.public_id,sig.thesis,sig.horizon,sig.invalidation,sig.disclosure,
                   sig.created_at,u.username,u.display_name,s.*
            FROM signals sig
            JOIN users u ON u.id=sig.user_id
            JOIN scan_snapshots s ON s.id=sig.snapshot_id
            WHERE sig.status='public'
            ORDER BY sig.created_at DESC LIMIT 50
            """
        ).fetchall()
    return templates.TemplateResponse(
        request=request,
        name="home.html",
        context=page_context(request, runner_session, signals=[dict(row) for row in rows]),
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


def pulse_data() -> dict[str, Any]:
    cutoff = iso(now() - timedelta(days=3))
    gap_cutoff = iso(now() - timedelta(hours=3))
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
        gap_rows = db.execute(
            """
            SELECT s.* FROM scan_snapshots s
            JOIN (
                SELECT ticker,MAX(captured_at) AS captured_at
                FROM scan_snapshots WHERE captured_at>? GROUP BY ticker
            ) latest ON latest.ticker=s.ticker AND latest.captured_at=s.captured_at
            ORDER BY s.score DESC LIMIT 40
            """,
            (gap_cutoff,),
        ).fetchall()
        state_rows = db.execute(
            "SELECT key,value FROM worker_state WHERE key LIKE 'edgar_%'"
        ).fetchall()

    grouped: dict[str, dict[str, Any]] = {}
    penny_filings = 0
    for raw in filing_rows:
        event = _intelligence_evidence(dict(raw))
        if event.get("price") is None or float(event["price"]) > 5:
            continue
        penny_filings += 1
        ticker = event["ticker"]
        if ticker in grouped:
            grouped[ticker]["event_count"] += 1
            continue
        event.update(
            {
                "coin_label": ticker[:2],
                "coin_tone": _coin_tone(ticker),
                "pulse_label": _pulse_label(event),
                "event_count": 1,
                "source": "sec",
                "event_at": event["filed_at"],
            }
        )
        grouped[ticker] = event

    unexplained = 0
    for raw in gap_rows:
        snapshot = dict(raw)
        ticker = snapshot["ticker"]
        if ticker in grouped:
            continue
        relative_volume = snapshot.get("relative_volume")
        if abs(float(snapshot["change_pct"])) < 5 and (
            relative_volume is None or float(relative_volume) < 2
        ):
            continue
        unexplained += 1
        grouped[ticker] = {
            **snapshot,
            "company": ticker,
            "kind": "No recent SEC catalyst",
            "sentiment": "gap",
            "form": "",
            "coin_label": ticker[:2],
            "coin_tone": _coin_tone(ticker),
            "pulse_label": "No recent SEC filing",
            "event_count": 1,
            "source": "gap",
            "event_at": snapshot["captured_at"],
            "return_1h_pct": None,
            "return_1d_pct": None,
            "return_5d_pct": None,
        }

    rows = sorted(
        grouped.values(),
        key=lambda row: (float(row.get("score") or 0), str(row.get("event_at") or "")),
        reverse=True,
    )[:50]
    state = {row["key"]: row["value"] for row in state_rows}
    return {
        "rows": rows,
        "stats": {
            "live": len(rows),
            "unexplained": unexplained,
            "filings": penny_filings,
        },
        "updated_at": state.get("edgar_last_refresh"),
    }


@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="pulse.html",
        context=page_context(request, runner_session, pulse=pulse_data(), active_tab="pulse"),
    )


@app.get("/api/pulse")
def pulse_api() -> JSONResponse:
    return JSONResponse(pulse_data())


def _clean_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper().replace(".", "-")
    if not TICKER_RE.fullmatch(normalized):
        raise HTTPException(404, "Ticker not found")
    return normalized


def ticker_detail_data(ticker: str) -> dict[str, Any] | None:
    with connection() as db:
        filings = db.execute(
            """
            SELECT f.*,o.return_1h_pct,o.return_1d_pct,o.return_5d_pct,
                   o.observed_1h_at,o.observed_1d_at,o.observed_5d_at
            FROM sec_filings f
            LEFT JOIN sec_outcomes o ON o.accession=f.accession
            WHERE f.ticker=? AND f.created_at>?
            ORDER BY f.score DESC,f.filed_at DESC LIMIT 12
            """,
            (ticker, iso(now() - timedelta(days=30))),
        ).fetchall()
        company = db.execute(
            "SELECT name,exchange FROM sec_companies WHERE ticker=? LIMIT 1", (ticker,)
        ).fetchone()
        snapshot = db.execute(
            """
            SELECT * FROM scan_snapshots WHERE ticker=?
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
    if events:
        current = events[0]
    elif snapshot is not None:
        current = {
            **dict(snapshot),
            "ticker": ticker,
            "kind": "No recent SEC catalyst",
            "sentiment": "gap",
            "event_at": snapshot["captured_at"],
            "return_1h_pct": None,
            "return_1d_pct": None,
            "return_5d_pct": None,
        }
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
        }
    return {
        "ticker": ticker,
        "company": company["name"] if company else current.get("company", ticker),
        "exchange": company["exchange"] if company else "Listed US stock",
        "coin_label": ticker[:2],
        "coin_tone": _coin_tone(ticker),
        "current": current,
        "events": events,
    }


def ticker_chart_data(ticker: str) -> list[dict[str, Any]]:
    cached = CHART_CACHE.get(ticker)
    if cached and cached[0] > now() - timedelta(seconds=90):
        return cached[1]
    result = YahooMarketData(batch_size=1).intraday([ticker])
    frame = result.frames.get(ticker)
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
    CHART_CACHE[ticker] = (now(), points)
    return points


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
    watched = False
    if user:
        with connection() as db:
            watched = (
                db.execute(
                    "SELECT 1 FROM watches WHERE user_id=? AND ticker=?",
                    (user["id"], normalized),
                ).fetchone()
                is not None
            )
    return templates.TemplateResponse(
        request=request,
        name="ticker.html",
        context=page_context(
            request,
            runner_session,
            detail=detail,
            watched=watched,
            active_tab="pulse",
        ),
    )


@app.get("/api/t/{ticker}/chart")
async def ticker_chart_api(ticker: str) -> JSONResponse:
    normalized = _clean_ticker(ticker)
    points = await run_in_threadpool(ticker_chart_data, normalized)
    return JSONResponse({"ticker": normalized, "points": points})


def radar_data(user_id: str) -> list[dict[str, Any]]:
    with connection() as db:
        rows = db.execute(
            """
            SELECT w.ticker,w.created_at AS watched_at,
                   (SELECT c.name FROM sec_companies c WHERE c.ticker=w.ticker LIMIT 1) AS name,
                   (SELECT c.exchange FROM sec_companies c
                    WHERE c.ticker=w.ticker LIMIT 1) AS exchange,
                   f.accession,f.form,f.kind,f.sentiment,f.score,f.filed_at,
                   f.filing_url,f.actor,f.actor_title,f.transaction_codes,
                   f.transaction_shares,f.transaction_price,f.transaction_value,
                   f.price,f.change_pct
            FROM watches w
            LEFT JOIN sec_filings f ON f.accession=(
                SELECT sf.accession FROM sec_filings sf
                WHERE sf.ticker=w.ticker ORDER BY sf.filed_at DESC LIMIT 1
            )
            WHERE w.user_id=? ORDER BY COALESCE(f.filed_at,w.created_at) DESC
            """,
            (user_id,),
        ).fetchall()
    output: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        if item.get("accession"):
            item = _intelligence_evidence(item)
            item["pulse_label"] = _pulse_label(item)
        else:
            item["evidence_label"] = "Watching for changes"
            item["evidence_text"] = "No recent filing has matched this ticker yet."
            item["pulse_label"] = "Quiet"
        item["coin_label"] = item["ticker"][:2]
        item["coin_tone"] = _coin_tone(item["ticker"])
        output.append(item)
    return output


@app.get("/radar", response_class=HTMLResponse)
def radar_page(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    user = current_user(runner_session)
    if not user:
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(
        request=request,
        name="radar.html",
        context=page_context(
            request,
            runner_session,
            watches=radar_data(user["id"]),
            active_tab="radar",
        ),
    )


@app.post("/api/watch/{ticker}")
def toggle_watch(
    ticker: str,
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
    user = require_user(runner_session)
    normalized = _clean_ticker(ticker)
    with connection() as db:
        known = db.execute(
            """
            SELECT 1 FROM sec_companies WHERE ticker=?
            UNION SELECT 1 FROM sec_filings WHERE ticker=? LIMIT 1
            """,
            (normalized, normalized),
        ).fetchone()
        if not known:
            raise HTTPException(404, "Ticker not found")
        existing = db.execute(
            "SELECT 1 FROM watches WHERE user_id=? AND ticker=?",
            (user["id"], normalized),
        ).fetchone()
        if existing:
            db.execute(
                "DELETE FROM watches WHERE user_id=? AND ticker=?",
                (user["id"], normalized),
            )
            watched = False
        else:
            db.execute(
                "INSERT INTO watches(user_id,ticker,created_at) VALUES(?,?,?)",
                (user["id"], normalized, iso()),
            )
            watched = True
    return JSONResponse({"ticker": normalized, "watched": watched})


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


@app.get("/signup", response_class=HTMLResponse)
def signup_page(
    request: Request, runner_session: str | None = Cookie(default=None)
) -> HTMLResponse:
    if current_user(runner_session):
        return RedirectResponse("/", 303)
    return templates.TemplateResponse(
        request=request,
        name="auth.html",
        context=page_context(request, runner_session, mode="signup"),
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request, runner_session: str | None = Cookie(default=None)
) -> HTMLResponse:
    if current_user(runner_session):
        return RedirectResponse("/", 303)
    return templates.TemplateResponse(
        request=request,
        name="auth.html",
        context=page_context(request, runner_session, mode="login"),
    )


@app.post("/api/auth/register/options")
def register_options(payload: RegisterStart, request: Request) -> JSONResponse:
    require_origin(request)
    username = payload.username.strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(400, "Use 3–24 lowercase letters, numbers, or underscores.")
    user_id = str(uuid.uuid4())
    with connection() as db:
        existing = db.execute("SELECT status FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            raise HTTPException(409, "That username is already taken.")
        db.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            (user_id, username, payload.display_name.strip(), "pending", iso()),
        )
    options = generate_registration_options(
        rp_id=RP_ID,
        rp_name="Runner Watch",
        user_id=user_id.encode(),
        user_name=username,
        user_display_name=payload.display_name.strip(),
        timeout=60_000,
        attestation=AttestationConveyancePreference.NONE,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    flow_token = save_challenge("register", options.challenge, user_id)
    return JSONResponse({"flow_token": flow_token, "options": json.loads(options_to_json(options))})


@app.post("/api/auth/register/verify")
def register_verify(payload: PasskeyFinish, request: Request) -> JSONResponse:
    require_origin(request)
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


@app.post("/api/auth/logout")
def logout(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> JSONResponse:
    require_origin(request)
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
            SELECT ticker,kind,form,filing_url,filed_at,sentiment FROM sec_filings
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
    config = SCAN_MODES.get(mode)
    if not config:
        raise ValueError("Unknown scan mode")
    cached = SCAN_CACHE.get(mode)
    if cached and cached[0] > now() - timedelta(seconds=90):
        return {
            "rows": cached[1],
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
    )
    symbols = [entry.symbol for entry in entries]
    result = RunnerScanner(YahooMarketData(batch_size=60)).scan(
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
    output: list[dict[str, Any]] = []
    catalysts = recent_sec_catalysts([item.ticker for item in result.rows])
    with connection() as db:
        for item in result.rows:
            snapshot_id = secrets.token_urlsafe(10)
            catalyst = catalysts.get(item.ticker)
            values = {
                "id": snapshot_id,
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
                "dollar_volume": item.dollar_volume,
                "quote_time": item.quote_time.isoformat(),
                "signals_json": json.dumps(item.signals),
                "risks_json": json.dumps(item.risks),
                "captured_at": captured_at,
                "catalyst_kind": catalyst["kind"] if catalyst else None,
                "catalyst_form": catalyst["form"] if catalyst else None,
                "catalyst_url": catalyst["filing_url"] if catalyst else None,
                "catalyst_filed_at": catalyst["filed_at"] if catalyst else None,
                "catalyst_status": "matched_sec" if catalyst else "no_recent_sec",
            }
            db.execute(
                """
                INSERT INTO scan_snapshots VALUES(
                    :id,:ticker,:score,:stage,:session,:price,:change_pct,
                    :momentum_5m_pct,:momentum_15m_pct,:relative_volume,
                    :recent_relative_volume,:breakout_pct,:dollar_volume,:quote_time,
                    :signals_json,:risks_json,:captured_at
                )
                """,
                values,
            )
            output.append(values)
    SCAN_CACHE[mode] = (now(), output)
    return {
        "rows": output,
        "mode": mode,
        "label": config["label"],
        "cached": False,
        "candidates": len(symbols),
        "eligible": result.liquid_symbols,
        "scanned": result.scanned_symbols,
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
    require_user(runner_session)
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
                   u.display_name,s.*
            FROM signals sig JOIN users u ON u.id=sig.user_id
            JOIN scan_snapshots s ON s.id=sig.snapshot_id
            WHERE sig.public_id=? AND sig.status='public'
            """,
            (public_id,),
        ).fetchone()
    return row_dict(row)


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
    return JSONResponse({"ok": True})
