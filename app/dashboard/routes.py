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
from app.auto_capture import (
    COCO_CLASSES,
    get_current_config as get_auto_capture_config,
    save_config as save_auto_capture_config,
    trigger_test_fire as auto_capture_test_fire,
)
from app.config import settings
from app.database import get_db
from app.observability import (
    directory_size_bytes,
    disk_usage,
    process_uptime_seconds,
    recent_errors,
)
from app.runtime_settings import apply_security_config, get_security_config, save_security_config
from app.security import hash_password
from app.security import login_rate_limiter
from app.streaming import (
    frame_hubs,
    issue_camera_token,
    issue_worker_token,
    list_cameras,
    list_workers,
    revoke_camera,
    revoke_worker,
    worker_link,
)
from app.thumbnail_jobs import get_thumbnail_job_stats

log = logging.getLogger("sentinelCam.dashboard")
router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="templates")

AUDIT = logging.getLogger("sentinelCam.audit")


def _audit(event: str, **kwargs) -> None:
    AUDIT.info(json.dumps({"event": event, **kwargs, "timestamp": time.time()}))


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, user: User = Depends(require_admin)):
    return templates.TemplateResponse(request, "admin.html", {"request": request, "user": user})


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
            await conn.execute(
                "UPDATE users SET password_hash = ?, failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
                (pw_hash, user_id),
            )
            await conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
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


class SecuritySettingsRequest(BaseModel):
    login_rate_limit: int
    login_rate_limit_window_minutes: int
    lockout_threshold: int
    lockout_duration_minutes: int


class UnblockIpRequest(BaseModel):
    ip: str

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128:
            raise ValueError("ip is required")
        return value


@router.get("/api/admin/security")
async def get_security_status(admin: User = Depends(require_admin)):
    return JSONResponse(
        {
            "settings": get_security_config().as_dict(),
            "blocked_ips": login_rate_limiter.blocked_ips(),
        }
    )


@router.patch("/api/admin/security/settings")
async def update_security_settings(
    body: SecuritySettingsRequest,
    request: Request,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    try:
        config = await save_security_config(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    apply_security_config(config)
    _audit("admin.security.update", admin=admin.username, settings=config.as_dict())
    return JSONResponse({"ok": True, "settings": config.as_dict()})


@router.post("/api/admin/security/blocked-ips/unblock")
async def unblock_ip(
    body: UnblockIpRequest,
    request: Request,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    removed = login_rate_limiter.unblock(body.ip)
    _audit("admin.security.unblock_ip", admin=admin.username, ip=body.ip, removed=removed)
    return JSONResponse({"ok": True, "removed": removed})


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


async def _measure_database() -> dict[str, object]:
    """Round-trip a trivial query to confirm the pool is healthy."""
    started = time.perf_counter()
    try:
        async with get_db() as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        return {
            "host": settings.postgres_host,
            "port": settings.postgres_port,
            "db": settings.postgres_db,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "host": settings.postgres_host,
        "port": settings.postgres_port,
        "db": settings.postgres_db,
        "ok": True,
        "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
    }


async def _count_active_sessions() -> int:
    try:
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS c FROM sessions WHERE expires_at > ?",
                (time.time(),),
            )
            row = await cursor.fetchone()
    except Exception:
        return -1
    if row is None:
        return 0
    try:
        return int(row["c"])
    except (TypeError, KeyError, ValueError):
        try:
            return int(row[0])
        except Exception:
            return -1


@router.get("/api/admin/ops")
async def admin_ops(admin: User = Depends(require_admin)):
    db_status = await _measure_database()
    storage_path = settings.local_storage_path
    worker_state = worker_link.status()
    from app.streaming import webrtc as _webrtc
    return JSONResponse(
        {
            "uptime_seconds": process_uptime_seconds(),
            "thumbnail": get_thumbnail_job_stats(),
            "storage": {
                "type": "local",
                "path": storage_path,
                "disk": disk_usage(storage_path),
                "recordings_bytes": directory_size_bytes(storage_path),
            },
            "database": db_status,
            "sessions": {
                "active": await _count_active_sessions(),
            },
            "worker": worker_state,
            "hubs": frame_hubs.all_stats(),
            "webrtc": {"viewers": _webrtc.active_viewers()},
            "errors": recent_errors(limit=50),
        }
    )


# ---------------------------------------------------------------------------
#  Camera / worker token management (admin-only)
# ---------------------------------------------------------------------------


class CameraCreateRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 80:
            raise ValueError("name must be 1-80 chars")
        return value


@router.get("/api/admin/cameras")
async def admin_list_cameras(admin: User = Depends(require_admin)):
    return JSONResponse({"items": await list_cameras()})


@router.post("/api/admin/cameras", status_code=201)
async def admin_create_camera(
    body: CameraCreateRequest,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    try:
        cam_id, token = await issue_camera_token(body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        if "UNIQUE" in str(exc) or "duplicate key" in str(exc).lower():
            raise HTTPException(409, "Camera name already exists")
        raise
    _audit("admin.camera.create", admin=admin.username, camera_id=cam_id, name=body.name)
    # The plaintext token is returned exactly once; the DB only stores the hash.
    return JSONResponse({"ok": True, "id": cam_id, "token": token}, status_code=201)


@router.delete("/api/admin/cameras/{cam_id}")
async def admin_revoke_camera(
    cam_id: int,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    removed = await revoke_camera(cam_id)
    if not removed:
        raise HTTPException(404, "Camera not found or already revoked")
    _audit("admin.camera.revoke", admin=admin.username, camera_id=cam_id)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
#  Auto-recording (admin-only)
# ---------------------------------------------------------------------------


class AutoCaptureBody(BaseModel):
    enabled: bool = False
    classes: list[str] = []
    cooldown_s: int = 30
    duration_s: int = 15
    mode: str = "clip"


@router.get("/api/admin/auto-capture")
async def admin_get_auto_capture(admin: User = Depends(require_admin)):
    return JSONResponse(
        {
            "config": get_auto_capture_config().as_dict(),
            "classes_catalog": list(COCO_CLASSES),
        }
    )


@router.patch("/api/admin/auto-capture")
async def admin_patch_auto_capture(
    body: AutoCaptureBody,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    try:
        config = await save_auto_capture_config(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    _audit("admin.auto_capture.update", admin=admin.username, config=config.as_dict())
    return JSONResponse({"ok": True, "config": config.as_dict()})


class AutoCaptureTestBody(BaseModel):
    camera_id: int
    trigger: str = "person"

    @field_validator("trigger")
    @classmethod
    def _trim(cls, value: str) -> str:
        value = (value or "").strip()
        if not value or len(value) > 64:
            raise ValueError("trigger must be 1-64 chars")
        return value


@router.post("/api/admin/auto-capture/test-fire", status_code=202)
async def admin_auto_capture_test_fire(
    body: AutoCaptureTestBody,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    await auto_capture_test_fire(body.camera_id, body.trigger)
    _audit(
        "admin.auto_capture.test_fire",
        admin=admin.username,
        camera_id=body.camera_id,
        trigger=body.trigger,
    )
    return JSONResponse({"ok": True})


class WorkerCreateRequest(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 80:
            raise ValueError("name must be 1-80 chars")
        return value


@router.get("/api/admin/workers")
async def admin_list_workers(admin: User = Depends(require_admin)):
    return JSONResponse({"items": await list_workers(), "connection": worker_link.status()})


@router.post("/api/admin/workers", status_code=201)
async def admin_create_worker(
    body: WorkerCreateRequest,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    try:
        worker_id, token = await issue_worker_token(body.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        if "UNIQUE" in str(exc) or "duplicate key" in str(exc).lower():
            raise HTTPException(409, "Worker name already exists")
        raise
    _audit("admin.worker.create", admin=admin.username, worker_id=worker_id, name=body.name)
    return JSONResponse({"ok": True, "id": worker_id, "token": token}, status_code=201)


@router.delete("/api/admin/workers/{worker_id}")
async def admin_revoke_worker(
    worker_id: int,
    admin: User = Depends(require_admin),
    _csrf=Depends(check_csrf),
):
    removed = await revoke_worker(worker_id)
    if not removed:
        raise HTTPException(404, "Worker not found or already revoked")
    _audit("admin.worker.revoke", admin=admin.username, worker_id=worker_id)
    return JSONResponse({"ok": True})
