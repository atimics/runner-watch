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

APP_ORIGIN = os.getenv("APP_ORIGIN", "http://localhost:8080").rstrip("/")
RP_ID = os.getenv("RP_ID", "localhost")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
ROOT = Path(os.getenv("RUNNER_ROOT", Path.cwd()))
SESSION_COOKIE = "runner_session"
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,24}$")
SCAN_MODES = {
    "penny": {"label": "Penny stocks", "min_price": 0.20, "max_price": 5.00},
    "low_price": {"label": "Low-priced small caps", "min_price": 0.20, "max_price": 20.00},
}
SCAN_CACHE: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}

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


@app.on_event("startup")
async def startup() -> None:
    init_db()
    app.state.edgar_task = asyncio.create_task(edgar_worker())


@app.on_event("shutdown")
async def shutdown() -> None:
    task = getattr(app.state, "edgar_task", None)
    if task:
        task.cancel()
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


@app.get("/", response_class=HTMLResponse)
def home(
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


def intelligence_data() -> dict[str, Any]:
    cutoff = iso(now() - timedelta(days=3))
    with connection() as db:
        rows = db.execute(
            """
            SELECT * FROM sec_filings WHERE created_at>?
            ORDER BY score DESC, filed_at DESC LIMIT 120
            """,
            (cutoff,),
        ).fetchall()
        state_rows = db.execute(
            "SELECT key,value,updated_at FROM worker_state WHERE key LIKE 'edgar_%'"
        ).fetchall()
    return {
        "rows": [dict(row) for row in rows],
        "state": {row["key"]: row["value"] for row in state_rows},
    }


@app.get("/intelligence", response_class=HTMLResponse)
def intelligence_page(
    request: Request,
    runner_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    data = intelligence_data()
    return templates.TemplateResponse(
        request=request,
        name="intelligence.html",
        context=page_context(request, runner_session, intelligence=data),
    )


@app.get("/api/intelligence")
def intelligence_api() -> JSONResponse:
    return JSONResponse(intelligence_data())


@app.get("/signup", response_class=HTMLResponse)
def signup_page(
    request: Request, runner_session: str | None = Cookie(default=None)
) -> HTMLResponse:
    if current_user(runner_session):
        return RedirectResponse("/scanner", 303)
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
        return RedirectResponse("/scanner", 303)
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
    response = JSONResponse({"ok": True, "redirect": "/scanner"})
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
    response = JSONResponse({"ok": True, "redirect": "/scanner"})
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
    with connection() as db:
        for item in result.rows:
            snapshot_id = secrets.token_urlsafe(10)
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
