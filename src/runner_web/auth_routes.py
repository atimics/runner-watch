from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
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
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from runner_web.pseudonyms import ensure_comment_avatar
from runner_web.request_security import safe_next_path


class PasskeyFinish(BaseModel):
    flow_token: str
    credential: dict[str, Any]


class RegisterOptionsPayload(BaseModel):
    invite_code: str = Field(default="", max_length=200)


@dataclass(frozen=True)
class AuthRouteDependencies:
    templates: Jinja2Templates
    page_context: Callable[..., dict[str, Any]]
    current_user: Callable[[str | None], dict[str, Any] | None]
    require_origin: Callable[[Request], None]
    require_user: Callable[[str | None], dict[str, Any]]
    require_recent_auth: Callable[[str | None], None]
    enforce_rate: Callable[..., None]
    create_session: Callable[..., None]
    mark_session_authenticated: Callable[[str, str], None]
    save_challenge: Callable[..., str]
    take_challenge: Callable[[str, str], dict[str, Any]]
    rp_id_for_request: Callable[[Request], str]
    origin_for_request: Callable[[Request], str]
    legacy_passkey_migration_available: Callable[[Request], bool]
    enum_value: Callable[[Any], str]
    token_hash: Callable[[str], str]
    iso: Callable[..., str]
    now: Callable[[], datetime]
    connection: Callable[[], Any]
    runners_origin: Callable[[], str]
    sports_origin: Callable[[], str]
    legacy_rp_id: Callable[[], str]
    registration_mode: Callable[[], str]
    registration_invite_codes: Callable[[], tuple[str, ...]]
    session_cookie: str
    cookie_domain: str | None


@dataclass(frozen=True)
class AuthRoutes:
    router: APIRouter
    legacy_openrouter_callback: Callable[..., Any]
    signup_page: Callable[..., Any]
    login_page: Callable[..., Any]
    webauthn_related_origins: Callable[..., Any]
    register_options: Callable[..., Any]
    register_verify: Callable[..., Any]
    login_options: Callable[..., Any]
    login_verify: Callable[..., Any]
    legacy_login_options: Callable[..., Any]
    legacy_login_verify: Callable[..., Any]
    add_passkey_page: Callable[..., Any]
    reauth_options: Callable[..., Any]
    reauth_verify: Callable[..., Any]
    add_passkey_options: Callable[..., Any]
    add_passkey_verify: Callable[..., Any]
    logout: Callable[..., Any]


def create_auth_routes(dependencies: AuthRouteDependencies) -> AuthRoutes:
    router = APIRouter()

    @router.get("/auth/openrouter/callback")
    def legacy_openrouter_callback() -> RedirectResponse:

        return RedirectResponse("/", 303)

    @router.get("/signup", response_class=HTMLResponse)
    def signup_page() -> RedirectResponse:
        return RedirectResponse("/login", 308)

    @router.get("/login", response_class=HTMLResponse)
    def login_page(
        request: Request, runner_session: str | None = Cookie(default=None)
    ) -> HTMLResponse:
        user = dependencies.current_user(runner_session)
        if user:
            return RedirectResponse("/", 303)
        return dependencies.templates.TemplateResponse(
            request=request,
            name="auth.html",
            context=dependencies.page_context(
                request,
                runner_session,
                resolved_user=None,
                next_path=safe_next_path(
                    request.query_params.get("next") if "query_string" in request.scope else None
                ),
            ),
        )

    @router.get("/.well-known/webauthn")
    def webauthn_related_origins() -> JSONResponse:

        origins = list(
            dict.fromkeys(
                origin
                for origin in (dependencies.runners_origin(), dependencies.sports_origin())
                if origin.startswith("https://")
            )
        )
        return JSONResponse(
            {"origins": origins},
            headers={"Cache-Control": "public, max-age=300"},
        )

    def _invite_hash(value: str) -> str:
        return hashlib.sha256(f"rati-registration-v1:{value.strip()}".encode()).hexdigest()

    def _validated_registration_invite(value: str) -> str | None:
        if dependencies.registration_mode() == "open":
            return None
        supplied_hash = _invite_hash(value)
        allowed_hashes = (_invite_hash(code) for code in dependencies.registration_invite_codes())
        if not any(secrets.compare_digest(supplied_hash, allowed) for allowed in allowed_hashes):
            raise HTTPException(403, "Invite code is invalid or has already been used.")
        return supplied_hash

    @router.post("/api/auth/register/options")
    def register_options(
        request: Request,
        payload: RegisterOptionsPayload | None = None,
    ) -> JSONResponse:
        dependencies.require_origin(request)
        dependencies.enforce_rate(request, "register-options", limit=8, seconds=600)
        dependencies.enforce_rate(request, "register-options-daily", limit=12, seconds=86400)
        invite_hash = _validated_registration_invite(
            (payload or RegisterOptionsPayload()).invite_code
        )
        user_id = str(uuid.uuid4())
        username = f"member_{user_id.replace('-', '')[:16]}"
        display_name = "Member"
        with dependencies.connection() as db:
            db.execute("DELETE FROM auth_challenges WHERE expires_at<=?", (dependencies.iso(),))
            db.execute(
                """
                DELETE FROM users WHERE status='pending' AND created_at<?
                AND NOT EXISTS(SELECT 1 FROM passkeys p WHERE p.user_id=users.id)
                """,
                (dependencies.iso(dependencies.now() - timedelta(minutes=15)),),
            )
            pending_invite = None
            if invite_hash:
                pending_invite = db.execute(
                    """
                    SELECT id,username,display_name,status FROM users
                    WHERE registration_invite_hash=?
                    """,
                    (invite_hash,),
                ).fetchone()
            if pending_invite:
                if str(pending_invite["status"]) != "pending":
                    raise HTTPException(403, "Invite code is invalid or has already been used.")
                user_id = str(pending_invite["id"])
                username = str(pending_invite["username"])
                display_name = str(pending_invite["display_name"])
                db.execute(
                    "DELETE FROM auth_challenges WHERE kind='register' AND user_id=?",
                    (user_id,),
                )
            else:
                inserted = db.execute(
                    """
                    INSERT INTO users(
                        id,username,display_name,status,created_at,registration_invite_hash
                    ) VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING
                    """,
                    (user_id, username, display_name, "pending", dependencies.iso(), invite_hash),
                )
                if inserted.rowcount != 1:
                    raise HTTPException(403, "Invite code is invalid or has already been used.")
        options = generate_registration_options(
            rp_id=dependencies.rp_id_for_request(request),
            rp_name="RATi",
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
        flow_token = dependencies.save_challenge("register", options.challenge, user_id)
        return JSONResponse(
            {"flow_token": flow_token, "options": json.loads(options_to_json(options))}
        )

    @router.post("/api/auth/register/verify")
    def register_verify(payload: PasskeyFinish, request: Request) -> JSONResponse:
        dependencies.require_origin(request)
        dependencies.enforce_rate(request, "register-verify", limit=12, seconds=600)
        flow = dependencies.take_challenge(payload.flow_token, "register")
        try:
            verification = verify_registration_response(
                credential=payload.credential,
                expected_challenge=flow["challenge"],
                expected_rp_id=dependencies.rp_id_for_request(request),
                expected_origin=dependencies.origin_for_request(request),
                require_user_verification=True,
            )
        except Exception as exc:
            raise HTTPException(400, f"Passkey verification failed: {exc}") from exc
        response = JSONResponse({"ok": True, "redirect": "/"})
        transports = payload.credential.get("response", {}).get("transports", [])
        with dependencies.connection() as db:
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
                    dependencies.enum_value(verification.credential_device_type),
                    int(verification.credential_backed_up),
                    json.dumps(transports),
                    dependencies.iso(),
                ),
            )
            db.execute("UPDATE users SET status='active' WHERE id=?", (flow["user_id"],))
            ensure_comment_avatar(db, str(flow["user_id"]))
        dependencies.create_session(flow["user_id"], response)
        return response

    @router.post("/api/auth/login/options")
    def login_options(request: Request) -> JSONResponse:
        dependencies.require_origin(request)
        dependencies.enforce_rate(request, "login-options", limit=15, seconds=600)
        options = generate_authentication_options(
            rp_id=dependencies.rp_id_for_request(request),
            timeout=60_000,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        flow_token = dependencies.save_challenge("login", options.challenge)
        return JSONResponse(
            {"flow_token": flow_token, "options": json.loads(options_to_json(options))}
        )

    @router.post("/api/auth/login/verify")
    def login_verify(payload: PasskeyFinish, request: Request) -> JSONResponse:
        dependencies.require_origin(request)
        dependencies.enforce_rate(request, "login-verify", limit=20, seconds=600)
        flow = dependencies.take_challenge(payload.flow_token, "login")
        credential_id = base64url_to_bytes(payload.credential.get("id", ""))
        with dependencies.connection() as db:
            passkey = db.execute(
                "SELECT * FROM passkeys WHERE credential_id=?", (credential_id,)
            ).fetchone()
        if not passkey:
            raise HTTPException(404, "This passkey is not registered here.")
        try:
            verification = verify_authentication_response(
                credential=payload.credential,
                expected_challenge=flow["challenge"],
                expected_rp_id=dependencies.rp_id_for_request(request),
                expected_origin=dependencies.origin_for_request(request),
                credential_public_key=passkey["public_key"],
                credential_current_sign_count=passkey["sign_count"],
                require_user_verification=True,
            )
        except Exception as exc:
            raise HTTPException(400, f"Passkey login failed: {exc}") from exc
        with dependencies.connection() as db:
            db.execute(
                "UPDATE passkeys SET sign_count=?,last_used_at=? WHERE credential_id=?",
                (verification.new_sign_count, dependencies.iso(), credential_id),
            )
        response = JSONResponse({"ok": True, "redirect": "/"})
        dependencies.create_session(passkey["user_id"], response)
        return response

    @router.post("/api/auth/login/legacy/options")
    def legacy_login_options(request: Request) -> JSONResponse:

        dependencies.require_origin(request)
        if not dependencies.legacy_passkey_migration_available(request):
            raise HTTPException(404, "Legacy passkey migration is not available here.")
        dependencies.enforce_rate(request, "legacy-login-options", limit=10, seconds=600)
        options = generate_authentication_options(
            rp_id=dependencies.legacy_rp_id(),
            timeout=60_000,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        flow_token = dependencies.save_challenge("login_legacy", options.challenge)
        return JSONResponse(
            {"flow_token": flow_token, "options": json.loads(options_to_json(options))}
        )

    @router.post("/api/auth/login/legacy/verify")
    def legacy_login_verify(payload: PasskeyFinish, request: Request) -> JSONResponse:

        dependencies.require_origin(request)
        if not dependencies.legacy_passkey_migration_available(request):
            raise HTTPException(404, "Legacy passkey migration is not available here.")
        dependencies.enforce_rate(request, "legacy-login-verify", limit=12, seconds=600)
        flow = dependencies.take_challenge(payload.flow_token, "login_legacy")
        credential_id = base64url_to_bytes(payload.credential.get("id", ""))
        with dependencies.connection() as db:
            passkey = db.execute(
                "SELECT * FROM passkeys WHERE credential_id=?", (credential_id,)
            ).fetchone()
        if not passkey:
            raise HTTPException(404, "This passkey is not registered on the old site.")
        try:
            verification = verify_authentication_response(
                credential=payload.credential,
                expected_challenge=flow["challenge"],
                expected_rp_id=dependencies.legacy_rp_id(),
                expected_origin=dependencies.origin_for_request(request),
                credential_public_key=passkey["public_key"],
                credential_current_sign_count=passkey["sign_count"],
                require_user_verification=True,
            )
        except Exception as exc:
            raise HTTPException(400, f"Legacy passkey login failed: {exc}") from exc
        with dependencies.connection() as db:
            db.execute(
                "UPDATE passkeys SET sign_count=?,last_used_at=? WHERE credential_id=?",
                (verification.new_sign_count, dependencies.iso(), credential_id),
            )
        response = JSONResponse({"ok": True, "redirect": "/settings/passkey?migrate=1"})
        dependencies.create_session(passkey["user_id"], response)
        return response

    @router.get("/settings/passkey", response_class=HTMLResponse)
    def add_passkey_page(
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> HTMLResponse:
        user = dependencies.current_user(runner_session)
        if not user:
            return RedirectResponse("/login", 303)
        return dependencies.templates.TemplateResponse(
            request=request,
            name="passkey_add.html",
            context=dependencies.page_context(
                request,
                runner_session,
                resolved_user=user,
                migrating_legacy_passkey=request.query_params.get("migrate") == "1",
            ),
        )

    @router.post("/api/auth/reauth/options")
    def reauth_options(
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        dependencies.enforce_rate(
            request, "reauth-options", limit=8, seconds=600, subject=user["id"]
        )
        with dependencies.connection() as db:
            credential_rows = db.execute(
                "SELECT credential_id FROM passkeys WHERE user_id=? ORDER BY created_at",
                (user["id"],),
            ).fetchall()
        if not credential_rows:
            raise HTTPException(409, "This account has no passkey available for verification.")
        options = generate_authentication_options(
            rp_id=dependencies.rp_id_for_request(request),
            timeout=60_000,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=bytes(row["credential_id"]))
                for row in credential_rows
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        flow_token = dependencies.save_challenge("reauth", options.challenge, user["id"])
        return JSONResponse(
            {"flow_token": flow_token, "options": json.loads(options_to_json(options))}
        )

    @router.post("/api/auth/reauth/verify")
    def reauth_verify(
        payload: PasskeyFinish,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        dependencies.enforce_rate(
            request, "reauth-verify", limit=10, seconds=600, subject=user["id"]
        )
        flow = dependencies.take_challenge(payload.flow_token, "reauth")
        if flow["user_id"] != user["id"]:
            raise HTTPException(403, "Passkey request does not match this account.")
        credential_id = base64url_to_bytes(payload.credential.get("id", ""))
        with dependencies.connection() as db:
            passkey = db.execute(
                "SELECT * FROM passkeys WHERE credential_id=? AND user_id=?",
                (credential_id, user["id"]),
            ).fetchone()
        if not passkey:
            raise HTTPException(403, "Use a passkey that is already registered to this account.")
        try:
            verification = verify_authentication_response(
                credential=payload.credential,
                expected_challenge=flow["challenge"],
                expected_rp_id=dependencies.rp_id_for_request(request),
                expected_origin=dependencies.origin_for_request(request),
                credential_public_key=passkey["public_key"],
                credential_current_sign_count=passkey["sign_count"],
                require_user_verification=True,
            )
        except Exception as exc:
            raise HTTPException(400, f"Passkey verification failed: {exc}") from exc
        with dependencies.connection() as db:
            db.execute(
                "UPDATE passkeys SET sign_count=?,last_used_at=? WHERE credential_id=?",
                (verification.new_sign_count, dependencies.iso(), credential_id),
            )
        dependencies.mark_session_authenticated(str(runner_session), str(user["id"]))
        return JSONResponse({"ok": True})

    @router.post("/api/auth/passkey/options")
    def add_passkey_options(
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        dependencies.require_recent_auth(runner_session)
        dependencies.enforce_rate(request, "add-passkey", limit=6, seconds=600, subject=user["id"])
        options = generate_registration_options(
            rp_id=dependencies.rp_id_for_request(request),
            rp_name="RATi",
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
        flow_token = dependencies.save_challenge("add_passkey", options.challenge, user["id"])
        return JSONResponse(
            {"flow_token": flow_token, "options": json.loads(options_to_json(options))}
        )

    @router.post("/api/auth/passkey/verify")
    def add_passkey_verify(
        payload: PasskeyFinish,
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        user = dependencies.require_user(runner_session)
        dependencies.require_recent_auth(runner_session)
        dependencies.enforce_rate(
            request, "add-passkey-verify", limit=8, seconds=600, subject=user["id"]
        )
        flow = dependencies.take_challenge(payload.flow_token, "add_passkey")
        if flow["user_id"] != user["id"]:
            raise HTTPException(403, "Passkey request does not match this account.")
        try:
            verification = verify_registration_response(
                credential=payload.credential,
                expected_challenge=flow["challenge"],
                expected_rp_id=dependencies.rp_id_for_request(request),
                expected_origin=dependencies.origin_for_request(request),
                require_user_verification=True,
            )
        except Exception as exc:
            raise HTTPException(400, f"Passkey verification failed: {exc}") from exc
        transports = payload.credential.get("response", {}).get("transports", [])
        with dependencies.connection() as db:
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
                    dependencies.enum_value(verification.credential_device_type),
                    int(verification.credential_backed_up),
                    json.dumps(transports),
                    dependencies.iso(),
                ),
            )
        response = JSONResponse({"ok": True, "redirect": "/"})
        dependencies.create_session(str(user["id"]), response, revoke_existing=True)
        return response

    @router.post("/api/auth/logout")
    def logout(
        request: Request,
        runner_session: str | None = Cookie(default=None),
    ) -> JSONResponse:
        dependencies.require_origin(request)
        dependencies.enforce_rate(request, "logout", limit=20, seconds=60)
        if runner_session:
            with dependencies.connection() as db:
                db.execute(
                    "DELETE FROM sessions WHERE token_hash=?",
                    (dependencies.token_hash(runner_session),),
                )
        response = JSONResponse({"ok": True, "redirect": "/"})
        response.delete_cookie(
            dependencies.session_cookie, path="/", domain=dependencies.cookie_domain
        )
        return response

    return AuthRoutes(
        router=router,
        legacy_openrouter_callback=legacy_openrouter_callback,
        signup_page=signup_page,
        login_page=login_page,
        webauthn_related_origins=webauthn_related_origins,
        register_options=register_options,
        register_verify=register_verify,
        login_options=login_options,
        login_verify=login_verify,
        legacy_login_options=legacy_login_options,
        legacy_login_verify=legacy_login_verify,
        add_passkey_page=add_passkey_page,
        reauth_options=reauth_options,
        reauth_verify=reauth_verify,
        add_passkey_options=add_passkey_options,
        add_passkey_verify=add_passkey_verify,
        logout=logout,
    )
