from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response, status

from app.database import get_db

log = logging.getLogger("sentinelCam.auth")


class User:
    def __init__(self, row: dict) -> None:
        self.id: int = row["id"]
        self.username: str = row["username"]
        self.role: str = row["role"]
        self.password_hash: str = row["password_hash"]
        self.failed_login_attempts: int = row["failed_login_attempts"]
        self.locked_until: Optional[float] = row["locked_until"]
        self.created_at: float = row["created_at"]
        self.last_login: Optional[float] = row["last_login"]


async def _get_session_user(request: Request) -> Optional[User]:
    import time
    from app.config import settings

    session_id = request.cookies.get("session")
    if not session_id:
        return None

    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT s.id, s.expires_at, u.id as uid, u.username, u.role, u.password_hash, "
            "u.failed_login_attempts, u.locked_until, u.created_at, u.last_login "
            "FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        now = time.time()
        if row["expires_at"] < now:
            await conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            await conn.commit()
            return None

        # Sliding window: extend session
        new_expires = now + settings.session_max_age_hours * 3600
        await conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE id = ?",
            (new_expires, session_id),
        )
        await conn.commit()

        return User({
            "id": row["uid"],
            "username": row["username"],
            "role": row["role"],
            "password_hash": row["password_hash"],
            "failed_login_attempts": row["failed_login_attempts"],
            "locked_until": row["locked_until"],
            "created_at": row["created_at"],
            "last_login": row["last_login"],
        })


async def get_current_user(request: Request) -> User:
    user = await _get_session_user(request)
    if user is None:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            from fastapi.responses import RedirectResponse
            raise HTTPException(
                status_code=status.HTTP_307_TEMPORARY_REDIRECT,
                headers={"Location": "/auth/login"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"ok": False, "error": "Authentication required"},
        )
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"ok": False, "error": "Admin role required"},
        )
    return user


async def check_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    csrf_cookie = request.cookies.get("csrf_token")
    csrf_header = request.headers.get("x-csrf-token")
    if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"ok": False, "error": "CSRF token mismatch"},
        )
