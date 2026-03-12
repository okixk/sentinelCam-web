from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from app.auth.dependencies import User, check_csrf, require_admin
from app.config import settings
from app.database import get_db
from app.security import hash_password

log = logging.getLogger("sentinelCam.dashboard")
router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="templates")

AUDIT = logging.getLogger("sentinelCam.audit")


def _audit(event: str, **kwargs) -> None:
    AUDIT.info(json.dumps({"event": event, **kwargs, "timestamp": time.time()}))


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user: User = Depends(require_admin)):
    return templates.TemplateResponse("admin.html", {"request": request, "user": user})


@router.get("/api/admin/users")
async def list_users(user: User = Depends(require_admin)):
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT id, username, role, created_at, last_login, failed_login_attempts, locked_until FROM users ORDER BY id"
        )
        rows = await cursor.fetchall()
    return JSONResponse([dict(r) for r in rows])


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip()
        if not v or len(v) > 64:
            raise ValueError("username must be 1-64 chars")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < settings.min_password_length:
            raise ValueError(f"password must be at least {settings.min_password_length} chars")
        return v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in ("admin", "viewer"):
            raise ValueError("role must be admin or viewer")
        return v


@router.post("/api/admin/users", status_code=201)
async def create_user(
    body: CreateUserRequest,
    request: Request,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    pw_hash = hash_password(body.password)
    async with get_db() as conn:
        try:
            cursor = await conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?) RETURNING id",
                (body.username, pw_hash, body.role),
            )
            new_row = await cursor.fetchone()
            await conn.commit()
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "Username already exists")
            raise

    _audit("admin.user.create", admin=admin.username, new_user=body.username, role=body.role)
    return JSONResponse({"ok": True, "id": new_row["id"]}, status_code=201)


class PatchUserRequest(BaseModel):
    role: str | None = None
    password: str | None = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, v) -> str | None:
        if v is not None and v not in ("admin", "viewer"):
            raise ValueError("role must be admin or viewer")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v) -> str | None:
        if v is not None and len(v) < settings.min_password_length:
            raise ValueError(f"password must be at least {settings.min_password_length} chars")
        return v


@router.patch("/api/admin/users/{user_id}")
async def update_user(
    user_id: int,
    body: PatchUserRequest,
    request: Request,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    async with get_db() as conn:
        cursor = await conn.execute("SELECT id, username FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "User not found")

        if body.role is not None:
            if user_id == admin.id:
                raise HTTPException(403, "Cannot change your own role")
            await conn.execute("UPDATE users SET role = ? WHERE id = ?", (body.role, user_id))
            _audit("admin.user.role_change", admin=admin.username, target=row["username"], new_role=body.role)

        if body.password is not None:
            pw_hash = hash_password(body.password)
            await conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id))
            _audit("admin.user.password_reset", admin=admin.username, target=row["username"])

        await conn.commit()

    return JSONResponse({"ok": True})


@router.delete("/api/admin/users/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    if user_id == admin.id:
        raise HTTPException(403, "Cannot delete yourself")

    async with get_db() as conn:
        cursor = await conn.execute("SELECT username FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "User not found")
        await conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await conn.commit()

    _audit("admin.user.delete", admin=admin.username, target=row["username"])
    return JSONResponse({"ok": True})


@router.get("/api/admin/sessions")
async def list_sessions(admin: User = Depends(require_admin)):
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT s.id, s.user_id, u.username, s.created_at, s.expires_at, s.ip_address, s.user_agent "
            "FROM sessions s JOIN users u ON s.user_id = u.id "
            "WHERE s.expires_at > ? ORDER BY s.created_at DESC",
            (time.time(),),
        )
        rows = await cursor.fetchall()
    return JSONResponse([dict(r) for r in rows])


@router.delete("/api/admin/sessions/{session_id}")
async def revoke_session(
    session_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    async with get_db() as conn:
        cursor = await conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Session not found")
        await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await conn.commit()

    _audit("admin.session.revoke", admin=admin.username, session_id=session_id)
    return JSONResponse({"ok": True})
