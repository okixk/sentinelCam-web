from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from app.auth.dependencies import User, check_csrf, get_current_user, _get_session_user
from app.config import settings
from app.database import get_db
from app.runtime_settings import get_security_config
from app.security import (
    dummy_verify,
    generate_csrf_token,
    generate_session_id,
    hash_password,
    login_rate_limiter,
    verify_password,
)

log = logging.getLogger("sentinelCam.auth")
router = APIRouter(prefix="/auth", tags=["auth"])
templates = Jinja2Templates(directory="templates")

AUDIT = logging.getLogger("sentinelCam.audit")


def _audit(event: str, **kwargs) -> None:
    AUDIT.info(json.dumps({"event": event, **kwargs, "timestamp": time.time()}))


def _request_is_secure(request: Request) -> bool:
    forwarded_proto = (request.headers.get("x-forwarded-proto", "") or "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _effective_origin(request: Request) -> str:
    """Origin as the browser sees it — must account for HTTPS termination upstream."""
    scheme = "https" if _request_is_secure(request) else request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    return f"{scheme}://{host}"


def _request_fingerprint(request: Request) -> str:
    ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "")[:256]
    payload = f"{ip}|{user_agent}".encode("utf-8", "ignore")
    return hashlib.sha256(payload).hexdigest()


_WEBAUTHN_CEREMONY_COOKIE = "wa_ceremony"


def _webauthn_registration_key(request: Request, user_id: int) -> str:
    # Registration runs while the user is already logged in, so the session
    # cookie binds the ceremony tightly to one browser.
    session_id = request.cookies.get("session") or _request_fingerprint(request)
    return f"reg:{user_id}:{session_id}"


def _ensure_ceremony_token(request: Request) -> str:
    token = request.cookies.get(_WEBAUTHN_CEREMONY_COOKIE) or ""
    if len(token) < 32 or not token.replace("-", "").replace("_", "").isalnum():
        token = secrets.token_urlsafe(32)
        request.state.new_ceremony_token = token
    return token


def _webauthn_login_key(token: str, username: str) -> str:
    return f"auth:{username}:{token}"


def _attach_ceremony_cookie(request: Request, response: Response) -> None:
    token = getattr(request.state, "new_ceremony_token", None)
    if not token:
        return
    response.set_cookie(
        _WEBAUTHN_CEREMONY_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=_request_is_secure(request),
        path="/auth",
        max_age=600,
    )


def _set_session_cookies(request: Request, response: Response, session_id: str, csrf_token: str) -> None:
    secure = _request_is_secure(request)
    response.set_cookie(
        "session",
        session_id,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/",
        max_age=settings.session_max_age_hours * 3600,
    )
    response.set_cookie(
        "csrf_token",
        csrf_token,
        httponly=False,
        samesite="strict",
        secure=secure,
        path="/",
        max_age=settings.session_max_age_hours * 3600,
    )


def _clear_session_cookies(request: Request, response: Response) -> None:
    secure = _request_is_secure(request)
    request.state.skip_session_cookie_refresh = True
    response.delete_cookie("session", path="/", samesite="strict", secure=secure)
    response.delete_cookie("csrf_token", path="/", samesite="strict", secure=secure)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await _get_session_user(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"request": request})


class LoginRequest(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def username_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("username required")
        if len(v) > 64:
            raise ValueError("username too long")
        return v

    @field_validator("password")
    @classmethod
    def password_not_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("password required")
        return v


@router.post("/login")
async def login(request: Request, body: LoginRequest):
    ip = request.client.host if request.client else "unknown"
    security_config = get_security_config()

    if not login_rate_limiter.is_allowed(ip):
        _audit("auth.login.ratelimit", ip=ip)
        return JSONResponse(
            {"ok": False, "error": "Too many login attempts. Try again later."},
            status_code=429,
        )

    login_rate_limiter.record_attempt(ip)

    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT id, username, password_hash, role, failed_login_attempts, locked_until "
            "FROM users WHERE username = ?",
            (body.username,),
        )
        user_row = await cursor.fetchone()

    if not user_row:
        # Burn the same argon2 cycle to keep timing indistinguishable from
        # the "user exists but password wrong" branch.
        dummy_verify()
        _audit("auth.login.failure", username=body.username, ip=ip, reason="user_not_found")
        return JSONResponse({"ok": False, "error": "Invalid credentials"}, status_code=401)

    now = time.time()
    locked_until = user_row["locked_until"]
    if locked_until and locked_until > now:
        _audit("auth.login.failure", username=body.username, ip=ip, reason="locked")
        return JSONResponse(
            {"ok": False, "error": "Invalid credentials"},
            status_code=401,
        )

    if not verify_password(body.password, user_row["password_hash"]):
        attempts = user_row["failed_login_attempts"] + 1
        new_locked_until = None
        if attempts >= security_config.lockout_threshold:
            new_locked_until = now + security_config.lockout_duration_minutes * 60
            _audit("auth.lockout", username=body.username, ip=ip)

        async with get_db() as conn:
            await conn.execute(
                "UPDATE users SET failed_login_attempts = ?, locked_until = ? WHERE username = ?",
                (attempts, new_locked_until, body.username),
            )
            await conn.commit()

        _audit("auth.login.failure", username=body.username, ip=ip, reason="wrong_password")
        return JSONResponse({"ok": False, "error": "Invalid credentials"}, status_code=401)

    # Successful login
    session_id = generate_session_id()
    csrf_token = generate_csrf_token()
    expires_at = now + settings.session_max_age_hours * 3600
    user_agent = request.headers.get("user-agent", "")[:256]

    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO sessions (id, user_id, expires_at, ip_address, user_agent) VALUES (?, ?, ?, ?, ?)",
            (session_id, user_row["id"], expires_at, ip, user_agent),
        )
        await conn.execute(
            "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, last_login = ? WHERE id = ?",
            (now, user_row["id"]),
        )
        await conn.commit()

    _audit("auth.login.success", username=body.username, ip=ip)

    response = JSONResponse({"ok": True, "redirect": "/"})
    _set_session_cookies(request, response, session_id, csrf_token)
    return response


@router.post("/logout")
async def logout(request: Request, user: User = Depends(get_current_user), _csrf=Depends(check_csrf)):
    session_id = request.cookies.get("session")
    if session_id:
        async with get_db() as conn:
            await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await conn.commit()
    _audit("auth.logout", username=user.username, ip=request.client.host if request.client else "unknown")
    response = RedirectResponse("/auth/login", status_code=302)
    _clear_session_cookies(request, response)
    return response


# WebAuthn routes
@router.post("/webauthn/register/begin")
async def webauthn_register_begin(
    request: Request,
    user: User = Depends(get_current_user),
    _csrf=Depends(check_csrf),
):
    from app.auth.webauthn import generate_registration_options, store_challenge
    import base64

    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT credential_id FROM webauthn_credentials WHERE user_id = ?",
            (user.id,),
        )
        rows = await cursor.fetchall()
        existing = [bytes(r["credential_id"]) for r in rows]

    rp_id = request.url.hostname or settings.webauthn_rp_id
    options = generate_registration_options(user.id, user.username, existing, rp_id=rp_id)
    challenge_b64 = options.get("challenge", "")
    from webauthn.helpers import base64url_to_bytes
    challenge_bytes = base64url_to_bytes(challenge_b64) if isinstance(challenge_b64, str) else challenge_b64
    store_challenge(_webauthn_registration_key(request, user.id), challenge_bytes)

    return JSONResponse(options)


@router.post("/webauthn/register/complete")
async def webauthn_register_complete(
    request: Request,
    user: User = Depends(get_current_user),
    _csrf=Depends(check_csrf),
):
    from app.auth.webauthn import verify_registration_response, pop_challenge
    body = await request.json()

    challenge = pop_challenge(_webauthn_registration_key(request, user.id))
    if not challenge:
        raise HTTPException(400, "No pending registration challenge")

    origin = _effective_origin(request)
    rp_id = request.url.hostname or settings.webauthn_rp_id
    try:
        result = verify_registration_response(
            challenge=challenge,
            response_data=body,
            rp_id=rp_id,
            origin=origin,
        )
    except Exception as e:
        log.warning("WebAuthn registration verification failed for %s: %s", user.username, e)
        raise HTTPException(400, "Registration verification failed")

    raw_name = body.get("name", "Passkey")
    if not isinstance(raw_name, str):
        raw_name = "Passkey"
    name = (raw_name.strip() or "Passkey")[:64]
    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO webauthn_credentials (user_id, credential_id, public_key, sign_count, name) "
            "VALUES (?, ?, ?, ?, ?)",
            (user.id, result["credential_id"], result["public_key"], result["sign_count"], name),
        )
        await conn.commit()

    _audit("auth.webauthn.register", username=user.username)
    return JSONResponse({"ok": True})


@router.post("/webauthn/login/begin")
async def webauthn_login_begin(request: Request):
    from app.auth.webauthn import generate_authentication_options, store_challenge
    ip = request.client.host if request.client else "unknown"

    if not login_rate_limiter.is_allowed(ip):
        _audit("auth.webauthn.ratelimit", ip=ip)
        raise HTTPException(429, "Too many login attempts. Try again later.")
    login_rate_limiter.record_attempt(ip)

    body = await request.json()
    username = (body.get("username") or "").strip()
    if not username or len(username) > 64:
        raise HTTPException(400, "username required")

    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT u.id, wc.credential_id FROM users u "
            "JOIN webauthn_credentials wc ON wc.user_id = u.id "
            "WHERE u.username = ?",
            (username,),
        )
        rows = await cursor.fetchall()

    # Always return a well-formed options payload, even if the user has no
    # passkeys or doesn't exist. This avoids leaking user existence; the
    # ceremony will fail later with a generic "Authentication failed".
    credentials = [{"credential_id": bytes(r["credential_id"])} for r in rows]
    rp_id = request.url.hostname or settings.webauthn_rp_id
    options = generate_authentication_options(credentials, rp_id=rp_id)

    from webauthn.helpers import base64url_to_bytes
    challenge_b64 = options.get("challenge", "")
    challenge_bytes = base64url_to_bytes(challenge_b64) if isinstance(challenge_b64, str) else challenge_b64
    ceremony_token = _ensure_ceremony_token(request)
    store_challenge(_webauthn_login_key(ceremony_token, username), challenge_bytes)

    response = JSONResponse(options)
    _attach_ceremony_cookie(request, response)
    return response


@router.post("/webauthn/login/complete")
async def webauthn_login_complete(request: Request):
    from app.auth.webauthn import verify_authentication_response, pop_challenge
    ip = request.client.host if request.client else "unknown"

    if not login_rate_limiter.is_allowed(ip):
        _audit("auth.webauthn.ratelimit", ip=ip)
        raise HTTPException(429, "Too many login attempts. Try again later.")
    login_rate_limiter.record_attempt(ip)

    body = await request.json()
    username = (body.get("username") or "").strip()
    credential_response = body.get("credential") or {}

    if not username or len(username) > 64 or not isinstance(credential_response, dict):
        raise HTTPException(400, "Authentication failed")

    ceremony_token = request.cookies.get(_WEBAUTHN_CEREMONY_COOKIE) or ""
    if not ceremony_token:
        raise HTTPException(400, "Authentication failed")
    challenge = pop_challenge(_webauthn_login_key(ceremony_token, username))
    if not challenge:
        raise HTTPException(400, "Authentication failed")

    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT wc.id, wc.credential_id, wc.public_key, wc.sign_count, u.id as uid, u.username, u.role "
            "FROM webauthn_credentials wc JOIN users u ON wc.user_id = u.id "
            "WHERE u.username = ?",
            (username,),
        )
        rows = await cursor.fetchall()

    if not rows:
        _audit("auth.webauthn.failure", username=username, ip=ip, reason="no_credentials")
        raise HTTPException(400, "Authentication failed")

    origin = _effective_origin(request)
    matched_row = None
    new_sign_count = 0
    rp_id = request.url.hostname or settings.webauthn_rp_id
    for row in rows:
        try:
            new_sign_count = verify_authentication_response(
                challenge=challenge,
                response_data=credential_response,
                credential_public_key=bytes(row["public_key"]),
                sign_count=row["sign_count"],
                rp_id=rp_id,
                origin=origin,
            )
            matched_row = row
            break
        except Exception:
            continue

    if not matched_row:
        _audit("auth.webauthn.failure", username=username, ip=ip, reason="verification_failed")
        raise HTTPException(400, "Authentication failed")

    # Update sign count
    async with get_db() as conn:
        await conn.execute(
            "UPDATE webauthn_credentials SET sign_count = ? WHERE id = ?",
            (new_sign_count, matched_row["id"]),
        )
        await conn.commit()

    # Create session
    now = time.time()
    session_id = generate_session_id()
    csrf_token = generate_csrf_token()
    expires_at = now + settings.session_max_age_hours * 3600
    ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "")[:256]

    async with get_db() as conn:
        await conn.execute(
            "INSERT INTO sessions (id, user_id, expires_at, ip_address, user_agent) VALUES (?, ?, ?, ?, ?)",
            (session_id, matched_row["uid"], expires_at, ip, user_agent),
        )
        await conn.execute(
            "UPDATE users SET last_login = ? WHERE id = ?",
            (now, matched_row["uid"]),
        )
        await conn.commit()

    _audit("auth.webauthn.login", username=username, ip=ip)

    response = JSONResponse({"ok": True, "redirect": "/"})
    _set_session_cookies(request, response, session_id, csrf_token)
    return response


@router.get("/webauthn/credentials")
async def list_my_passkeys(user: User = Depends(get_current_user)):
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT id, name, created_at, sign_count FROM webauthn_credentials WHERE user_id = ?",
            (user.id,),
        )
        rows = await cursor.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.delete("/webauthn/credentials/{cred_id}")
async def delete_passkey(
    cred_id: int,
    user: User = Depends(get_current_user),
    _csrf=Depends(check_csrf),
):
    async with get_db() as conn:
        cursor = await conn.execute(
            "DELETE FROM webauthn_credentials WHERE id = ? AND user_id = ?",
            (cred_id, user.id),
        )
        await conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(404, "Credential not found")
    _audit("auth.webauthn.delete", username=user.username, credential_id=cred_id)
    return JSONResponse({"ok": True})
