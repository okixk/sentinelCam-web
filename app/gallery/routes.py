from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import User, get_current_user
from app.database import get_db

router = APIRouter(tags=["gallery"])
templates = Jinja2Templates(directory="templates")


@router.get("/gallery", response_class=HTMLResponse)
async def gallery_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse("gallery.html", {"request": request, "user": user})


@router.get("/gallery/{recording_id}", response_class=HTMLResponse)
async def gallery_detail_page(
    recording_id: int,
    request: Request,
    user: User = Depends(get_current_user),
):
    async with get_db() as conn:
        if user.role == "admin":
            cursor = await conn.execute(
                "SELECT r.*, u.username FROM recordings r JOIN users u ON r.user_id = u.id WHERE r.id = ?",
                (recording_id,),
            )
        else:
            cursor = await conn.execute(
                "SELECT r.*, u.username FROM recordings r JOIN users u ON r.user_id = u.id "
                "WHERE r.id = ? AND r.user_id = ?",
                (recording_id, user.id),
            )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(404, "Recording not found")

    return templates.TemplateResponse(
        "gallery_detail.html",
        {"request": request, "user": user, "recording": dict(row)},
    )
