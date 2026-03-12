from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from app.auth.dependencies import User, check_csrf, get_current_user, _get_session_user
from app.config import settings
from app.database import get_db
from app.security import (
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


def _set_session_cookies(response: Response, session_id: str, csrf_token: str) -> None:
    response.set_cookie(
        "session",
        session_id,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=settings.session_max_age_hours * 3600,
    )
    response.set_cookie(
        "csrf_token",
        csrf_token,
        httponly=False,
        samesite="strict",
        path="/",
        max_age=settings.session_max_age_hours * 3600,
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie("session", path="/")
    response.delete_cookie("csrf_token", path="/")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = await _get_session_user(request)
    if user:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request})


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
        _audit("auth.login.failure", username=body.username, ip=ip, reason="user_not_found")
        return JSONResponse({"ok": False, "error": "Invalid credentials"}, status_code=401)

    now = time.time()
    locked_until = user_row["locked_until"]
    if locked_until and locked_until > now:
        minutes = int((locked_until - now) / 60) + 1
        _audit("auth.login.failure", username=body.username, ip=ip, reason="locked")
        return JSONResponse(
            {"ok": False, "error": f"Account locked. Try again in {minutes} minutes."},
            status_code=403,
        )

    if not verify_password(body.password, user_row["password_hash"]):
        attempts = user_row["failed_login_attempts"] + 1
        new_locked_until = None
        if attempts >= settings.lockout_threshold:
            new_locked_until = now + settings.lockout_duration_minutes * 60
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
    _set_session_cookies(response, session_id, csrf_token)
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
    _clear_session_cookies(response)
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
    store_challenge(f"reg_{user.id}", challenge_bytes)

    return JSONResponse(options)


@router.post("/webauthn/register/complete")
async def webauthn_register_complete(
    request: Request,
    user: User = Depends(get_current_user),
    _csrf=Depends(check_csrf),
):
    from app.auth.webauthn import verify_registration_response, pop_challenge
    body = await request.json()

    challenge = pop_challenge(f"reg_{user.id}")
    if not challenge:
        raise HTTPException(400, "No pending registration challenge")

    origin = f"{request.url.scheme}://{request.url.netloc}"
    rp_id = request.url.hostname or settings.webauthn_rp_id
    try:
        result = verify_registration_response(
            challenge=challenge,
            response_data=body,
            rp_id=rp_id,
            origin=origin,
        )
    except Exception as e:
        raise HTTPException(400, f"Registration verification failed: {e}")

    name = body.get("name", "Passkey")[:64]
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
    body = await request.json()
    username = body.get("username", "").strip()

    if not username:
        raise HTTPException(400, "username required")

    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT u.id, wc.credential_id FROM users u "
            "JOIN webauthn_credentials wc ON wc.user_id = u.id "
            "WHERE u.username = ?",
            (username,),
        )
        rows = await cursor.fetchall()

    if not rows:
        raise HTTPException(404, "No passkeys registered for this user")

    credentials = [{"credential_id": bytes(r["credential_id"])} for r in rows]
    rp_id = request.url.hostname or settings.webauthn_rp_id
    options = generate_authentication_options(credentials, rp_id=rp_id)

    from webauthn.helpers import base64url_to_bytes
    challenge_b64 = options.get("challenge", "")
    challenge_bytes = base64url_to_bytes(challenge_b64) if isinstance(challenge_b64, str) else challenge_b64
    store_challenge(f"auth_{username}", challenge_bytes)

    return JSONResponse(options)


@router.post("/webauthn/login/complete")
async def webauthn_login_complete(request: Request):
    from app.auth.webauthn import verify_authentication_response, pop_challenge
    body = await request.json()
    username = body.get("username", "").strip()
    credential_response = body.get("credential", {})

    challenge = pop_challenge(f"auth_{username}")
    if not challenge:
        raise HTTPException(400, "No pending authentication challenge")

    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT wc.id, wc.credential_id, wc.public_key, wc.sign_count, u.id as uid, u.username, u.role "
            "FROM webauthn_credentials wc JOIN users u ON wc.user_id = u.id "
            "WHERE u.username = ?",
            (username,),
        )
        rows = await cursor.fetchall()

    if not rows:
        raise HTTPException(404, "No credentials found")

    origin = f"{request.url.scheme}://{request.url.netloc}"
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
        raise HTTPException(400, "Authentication verification failed")

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
    _set_session_cookies(response, session_id, csrf_token)
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
