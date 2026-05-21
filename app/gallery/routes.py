from __future__ import annotations

from datetime import datetime
import json
from math import floor

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import User, get_current_user
from app.database import get_db
from app.thumbnail_jobs import schedule_thumbnail_warmup

router = APIRouter(tags=["gallery"])
templates = Jinja2Templates(directory="templates")


_ADMIN_PRESETS = {"shared", "mine", "videos", "auto"}
_VIEWER_PRESETS = {"shared", "mine", "videos"}


def _normalize_gallery_params(
    page: int,
    per_page: int,
    type: str | None,
    sort: str,
    q: str | None,
    preset: str | None,
    user: User | None = None,
) -> dict:
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or 20), 60))
    media_type = type if type in ("image", "video") else None
    sort = "oldest" if sort == "oldest" else "newest"
    q = (q or "").strip()[:80]
    allowed = _ADMIN_PRESETS if (user and user.role == "admin") else _VIEWER_PRESETS
    preset = preset if preset in allowed else None
    return {
        "page": page,
        "per_page": per_page,
        "type": media_type,
        "sort": sort,
        "q": q,
        "preset": preset,
    }


def _compose_where(conditions: list[str]) -> str:
    return "WHERE " + " AND ".join(conditions) if conditions else ""


def _gallery_access_conditions(user: User) -> tuple[list[str], list]:
    # Non-admins never see auto-recordings; among manual recordings they see
    # their own plus anything explicitly shared.
    if user.role == "admin":
        return [], []
    return ["r.auto = FALSE", "(r.user_id = ? OR r.shared = 1)"], [user.id]


def _gallery_where_clause(user: User, media_type: str | None, q: str, preset: str | None) -> tuple[str, list]:
    conditions, params = _gallery_access_conditions(user)

    if preset == "shared":
        conditions.append("r.shared = 1")
    elif preset == "mine":
        conditions.append("r.user_id = ?")
        params.append(user.id)
    elif preset == "videos":
        conditions.append("r.type = 'video'")
    elif preset == "auto":
        # Admin-only — _normalize_gallery_params already enforced that.
        conditions.append("r.auto = TRUE")

    if media_type:
        conditions.append("r.type = ?")
        params.append(media_type)

    if q:
        conditions.append(
            "("
            "CAST(r.id AS TEXT) LIKE ? OR "
            "LOWER(r.type) LIKE ? OR "
            "LOWER(COALESCE(u.username, '')) LIKE ? OR "
            "LOWER(COALESCE(r.filename, '')) LIKE ? OR "
            "LOWER(COALESCE(r.metadata, '')) LIKE ?"
            ")"
        )
        like = f"%{q.lower()}%"
        params.extend([like, like, like, like, like])

    return _compose_where(conditions), params


def _gallery_query_string(params: dict) -> str:
    parts: list[str] = []
    if params.get("page", 1) != 1:
        parts.append(f"page={params['page']}")
    if params.get("type"):
        parts.append(f"type={params['type']}")
    if params.get("sort") == "oldest":
        parts.append("sort=oldest")
    if params.get("q"):
        from urllib.parse import quote_plus

        parts.append(f"q={quote_plus(params['q'])}")
    if params.get("preset"):
        parts.append(f"preset={params['preset']}")
    return "&".join(parts)


@router.get("/gallery", response_class=HTMLResponse)
async def gallery_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(request, "gallery.html", {"request": request, "user": user})


@router.get("/gallery/data")
async def gallery_data(
    page: int = Query(1),
    per_page: int = Query(20),
    type: str | None = Query(None),
    sort: str = Query("newest"),
    q: str | None = Query(None),
    preset: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    params = _normalize_gallery_params(page, per_page, type, sort, q, preset, user)
    offset = (params["page"] - 1) * params["per_page"]
    order = "ASC" if params["sort"] == "oldest" else "DESC"
    where, query_params = _gallery_where_clause(user, params["type"], params["q"], params["preset"])

    async with get_db() as conn:
        cursor = await conn.execute(
            f"SELECT COUNT(*) FROM recordings r JOIN users u ON r.user_id = u.id {where}",
            query_params,
        )
        total_row = await cursor.fetchone()
        total = total_row[0] if total_row else 0

        cursor = await conn.execute(
            f"SELECT r.id, r.type, r.filename, r.overlay_filename, r.raw_filename, "
            f"r.size_bytes, r.duration_seconds, r.created_at, r.shared, r.metadata, "
            f"r.auto, r.auto_trigger, u.username "
            f"FROM recordings r JOIN users u ON r.user_id = u.id {where} "
            f"ORDER BY r.created_at {order}, r.id {order} LIMIT ? OFFSET ?",
            [*query_params, params["per_page"], offset],
        )
        rows = await cursor.fetchall()

    schedule_thumbnail_warmup(int(row["id"]) for row in rows[:12])

    return JSONResponse(
        {
            "items": [dict(r) for r in rows],
            "total": total,
            "page": params["page"],
            "per_page": params["per_page"],
            "pages": max(1, (total + params["per_page"] - 1) // params["per_page"]),
            "query": {
                "type": params["type"] or "",
                "sort": params["sort"],
                "q": params["q"],
                "preset": params["preset"] or "",
            },
        }
    )


@router.get("/gallery/{recording_id}", response_class=HTMLResponse)
async def gallery_detail_page(
    recording_id: int,
    request: Request,
    page: int = Query(1),
    type: str | None = Query(None),
    sort: str = Query("newest"),
    q: str | None = Query(None),
    preset: str | None = Query(None),
    user: User = Depends(get_current_user),
):
    params = _normalize_gallery_params(page, 20, type, sort, q, preset, user)
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT r.*, u.username FROM recordings r JOIN users u ON r.user_id = u.id WHERE r.id = ?",
            (recording_id,),
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(404, "Recording not found")
    if user.role != "admin":
        if row["auto"]:
            raise HTTPException(404, "Recording not found")
        if row["user_id"] != user.id and not row["shared"]:
            raise HTTPException(404, "Recording not found")

    where, query_params = _gallery_where_clause(user, params["type"], params["q"], params["preset"])
    order = "ASC" if params["sort"] == "oldest" else "DESC"
    async with get_db() as conn:
        cursor = await conn.execute(
            f"SELECT r.id FROM recordings r JOIN users u ON r.user_id = u.id {where} "
            f"ORDER BY r.created_at {order}, r.id {order}",
            query_params,
        )
        ids = [int(r["id"]) for r in await cursor.fetchall()]

        created_at = float(row["created_at"] or 0.0)
        hour_start = floor(created_at / 3600.0) * 3600.0
        hour_end = hour_start + 3600.0
        same_hour_conditions, same_hour_params = _gallery_access_conditions(user)
        same_hour_conditions.extend(
            [
                "r.id != ?",
                "r.created_at >= ?",
                "r.created_at < ?",
            ]
        )
        same_hour_params.extend([recording_id, hour_start, hour_end])
        same_hour_where = _compose_where(same_hour_conditions)
        cursor = await conn.execute(
            f"SELECT r.id, r.type, r.shared, r.created_at, u.username "
            f"FROM recordings r JOIN users u ON r.user_id = u.id {same_hour_where} "
            f"ORDER BY r.created_at ASC, r.id ASC LIMIT 12",
            same_hour_params,
        )
        same_hour_rows = await cursor.fetchall()

    prev_id = None
    next_id = None
    if recording_id in ids:
        idx = ids.index(recording_id)
        if idx > 0:
            prev_id = ids[idx - 1]
        if idx + 1 < len(ids):
            next_id = ids[idx + 1]

    back_query = _gallery_query_string(params)
    same_hour_label = datetime.fromtimestamp(hour_start).strftime("%Y-%m-%d %H:00")
    same_hour_end_label = datetime.fromtimestamp(hour_end - 1).strftime("%H:59")
    same_hour_items = []
    for r in same_hour_rows:
        item = dict(r)
        item["time_label"] = datetime.fromtimestamp(float(item["created_at"] or 0.0)).strftime("%H:%M:%S")
        item["type_label"] = "Image" if item["type"] == "image" else "Video"
        item["share_label"] = "Shared" if item["shared"] else "Private"
        same_hour_items.append(item)

    recording = dict(row)
    try:
        metadata = json.loads(recording.get("metadata") or "{}")
    except Exception:
        metadata = {}
    recording["description"] = str(metadata.get("description") or "").strip()

    return templates.TemplateResponse(
        request,
        "gallery_detail.html",
        {
            "request": request,
            "user": user,
            "recording": recording,
            "prev_id": prev_id,
            "next_id": next_id,
            "back_query": back_query,
            "detail_query": back_query,
            "same_hour_label": same_hour_label,
            "same_hour_end_label": same_hour_end_label,
            "same_hour_items": same_hour_items,
        },
    )
