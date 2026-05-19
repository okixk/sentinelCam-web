from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import PurePosixPath
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth.dependencies import User, check_csrf, get_current_user
from app.config import settings
from app.database import get_db
from app.storage import (
    delete_many,
    head_object,
    object_response_headers,
    recording_key,
    stream_object,
    thumbnail_key,
    upload_stream,
)
from app.thumbnail_jobs import ensure_thumbnail_from_row, schedule_thumbnail_warmup

log = logging.getLogger("sentinelCam.recording")
router = APIRouter(prefix="/api/recordings", tags=["recordings"])

AUDIT = logging.getLogger("sentinelCam.audit")


def _audit(event: str, **kwargs) -> None:
    AUDIT.info(json.dumps({"event": event, **kwargs, "timestamp": time.time()}))


ALLOWED_MIME_IMAGE = {"image/jpeg", "image/png"}
ALLOWED_MIME_VIDEO = {"video/webm", "video/mp4"}

EXTENSION_MAP = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "video/webm": "webm",
    "video/mp4": "mp4",
}

MEDIA_TYPE_MAP = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webm": "video/webm",
    "mp4": "video/mp4",
}


def _check_magic_bytes(data: bytes, mime: str) -> bool:
    if mime == "image/jpeg":
        return data[:3] == b"\xff\xd8\xff"
    if mime == "image/png":
        return data[:4] == b"\x89PNG"
    if mime == "video/webm":
        return data[:4] == b"\x1aE\xdf\xa3"
    if mime == "video/mp4":
        return len(data) >= 8 and data[4:8] == b"ftyp"
    return False


def _safe_filename_for_key(filename: str | None) -> str | None:
    """Reject path-traversal attempts before composing a storage key."""
    if not filename:
        return None
    s = str(filename)
    if "/" in s or "\\" in s or ".." in s:
        return None
    return s


@router.post("/upload")
async def upload_recording(
    request: Request,
    type: str = Form(...),
    overlay_file: UploadFile = File(...),
    raw_file: Optional[UploadFile] = File(None),
    duration: Optional[float] = Form(None),
    description: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    _csrf=Depends(check_csrf),
):
    if type not in ("image", "video"):
        raise HTTPException(400, "type must be image or video")

    max_bytes = settings.max_upload_size_mb * 1024 * 1024

    allowed_overlay = ALLOWED_MIME_IMAGE if type == "image" else ALLOWED_MIME_VIDEO
    overlay_mime = (overlay_file.content_type or "").lower()
    if overlay_mime not in allowed_overlay:
        raise HTTPException(415, f"Unsupported overlay content type for {type}")

    file_uuid = str(uuid.uuid4()).replace("-", "")
    ext = EXTENSION_MAP.get(overlay_mime, "bin")
    overlay_filename = f"{file_uuid}.{ext}"
    overlay_obj_key = recording_key(user.id, overlay_filename)

    overlay_size, overlay_head = await upload_stream(
        overlay_file, overlay_obj_key, max_bytes, content_type=overlay_mime
    )
    if not _check_magic_bytes(overlay_head, overlay_mime):
        await delete_many([overlay_obj_key])
        raise HTTPException(400, "File content does not match expected type")

    raw_filename: str | None = None
    raw_size = 0
    if raw_file is not None and getattr(raw_file, "filename", ""):
        raw_mime = (raw_file.content_type or "").lower()
        if raw_mime in allowed_overlay:
            raw_uuid = str(uuid.uuid4()).replace("-", "")
            raw_ext = EXTENSION_MAP.get(raw_mime, "jpg" if type == "image" else "webm")
            candidate_raw_filename = f"{raw_uuid}_raw.{raw_ext}"
            candidate_raw_key = recording_key(user.id, candidate_raw_filename)
            raw_size, raw_head = await upload_stream(
                raw_file, candidate_raw_key, max_bytes, content_type=raw_mime
            )
            if _check_magic_bytes(raw_head, raw_mime):
                raw_filename = candidate_raw_filename
            else:
                await delete_many([candidate_raw_key])
                raw_size = 0

    quota_bytes = settings.storage_quota_per_user_mb * 1024 * 1024
    add_bytes = overlay_size + raw_size

    clean_description = (description or "").strip()[:1000]
    metadata = json.dumps({"description": clean_description}) if clean_description else None

    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM recordings WHERE user_id = ?",
            (user.id,),
        )
        row = await cursor.fetchone()
        used = row[0] if row else 0
        if used + add_bytes > quota_bytes:
            await delete_many(
                [overlay_obj_key]
                + ([recording_key(user.id, raw_filename)] if raw_filename else [])
            )
            raise HTTPException(
                413,
                f"Storage quota exceeded ({settings.storage_quota_per_user_mb} MB limit)",
            )

        cursor = await conn.execute(
            "INSERT INTO recordings (user_id, type, filename, overlay_filename, raw_filename, size_bytes, duration_seconds, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                user.id,
                type,
                overlay_filename,
                overlay_filename,
                raw_filename,
                add_bytes,
                duration if type == "video" else None,
                metadata,
            ),
        )
        new_row = await cursor.fetchone()

    _audit("recording.upload", username=user.username, type=type, id=new_row["id"])
    schedule_thumbnail_warmup([new_row["id"]])
    return JSONResponse({"ok": True, "id": new_row["id"]}, status_code=201)


@router.get("")
async def list_recordings(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    type: Optional[str] = None,
    sort: str = "newest",
    q: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    if page < 1:
        page = 1
    if per_page < 1 or per_page > 100:
        per_page = 20
    offset = (page - 1) * per_page
    order = "DESC" if sort != "oldest" else "ASC"
    q = (q or "").strip()[:80]

    conditions: list[str] = []
    params: list = []

    if user.role != "admin":
        conditions.append("(r.user_id = ? OR r.shared = 1)")
        params.append(user.id)

    if type in ("image", "video"):
        conditions.append("r.type = ?")
        params.append(type)

    if q:
        like = f"%{q.lower()}%"
        conditions.append(
            "("
            "CAST(r.id AS TEXT) LIKE ? OR "
            "LOWER(r.type) LIKE ? OR "
            "LOWER(COALESCE(u.username, '')) LIKE ? OR "
            "LOWER(COALESCE(r.filename, '')) LIKE ? OR "
            "LOWER(COALESCE(r.metadata, '')) LIKE ?"
            ")"
        )
        params.extend([like, like, like, like, like])

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params_count = list(params)
    params.extend([per_page, offset])

    async with get_db() as conn:
        cursor = await conn.execute(
            f"SELECT COUNT(*) FROM recordings r JOIN users u ON r.user_id = u.id {where}",
            params_count,
        )
        total_row = await cursor.fetchone()
        total = total_row[0] if total_row else 0

        cursor = await conn.execute(
            f"SELECT r.id, r.type, r.filename, r.overlay_filename, r.raw_filename, "
            f"r.size_bytes, r.duration_seconds, r.created_at, r.shared, r.metadata, u.username "
            f"FROM recordings r JOIN users u ON r.user_id = u.id {where} "
            f"ORDER BY r.created_at {order}, r.id {order} LIMIT ? OFFSET ?",
            params,
        )
        rows = await cursor.fetchall()

    schedule_thumbnail_warmup(int(r["id"]) for r in rows[:12])

    return JSONResponse({
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
    })


@router.get("/{recording_id}")
async def get_recording(recording_id: int, user: User = Depends(get_current_user)):
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT r.*, u.username FROM recordings r JOIN users u ON r.user_id = u.id WHERE r.id = ?",
            (recording_id,),
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Recording not found")
    if row["user_id"] != user.id and user.role != "admin" and not row["shared"]:
        raise HTTPException(404, "Recording not found")
    return JSONResponse(dict(row))


@router.get("/{recording_id}/file")
async def serve_recording_file(
    recording_id: int,
    variant: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT r.user_id, r.filename, r.overlay_filename, r.raw_filename, r.type, r.shared "
            "FROM recordings r WHERE r.id = ?",
            (recording_id,),
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Recording not found")
    if row["user_id"] != user.id and user.role != "admin" and not row["shared"]:
        raise HTTPException(404, "Recording not found")

    if variant == "raw" and row["raw_filename"]:
        fname = row["raw_filename"]
    else:
        fname = row["overlay_filename"] or row["filename"]

    safe_name = _safe_filename_for_key(fname)
    if not safe_name:
        raise HTTPException(404, "Recording not found")

    ext = PurePosixPath(safe_name).suffix.lstrip(".").lower()
    media_type = MEDIA_TYPE_MAP.get(ext, "application/octet-stream")
    key = recording_key(int(row["user_id"]), safe_name)

    head = await head_object(key)

    headers = object_response_headers(head or {})
    headers.setdefault("Cache-Control", "private, max-age=300")

    async def iter_chunks():
        try:
            async for chunk in stream_object(key):
                yield chunk
        except FileNotFoundError:
            return

    if head is None:
        raise HTTPException(404, "File not found on disk")

    return StreamingResponse(iter_chunks(), media_type=media_type, headers=headers)


@router.get("/{recording_id}/thumbnail")
async def serve_thumbnail(recording_id: int, user: User = Depends(get_current_user)):
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT r.id, r.user_id, r.filename, r.overlay_filename, r.type, r.shared FROM recordings r WHERE r.id = ?",
            (recording_id,),
        )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Recording not found")
    if row["user_id"] != user.id and user.role != "admin" and not row["shared"]:
        raise HTTPException(404, "Recording not found")

    try:
        key = await ensure_thumbnail_from_row(row)
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    except Exception as e:
        raise HTTPException(500, f"Thumbnail generation failed: {e}")

    async def iter_chunks():
        try:
            async for chunk in stream_object(key):
                yield chunk
        except FileNotFoundError:
            return

    return StreamingResponse(
        iter_chunks(),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.delete("/{recording_id}")
async def delete_recording(
    recording_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    _csrf=Depends(check_csrf),
):
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT r.id, r.user_id, r.filename, r.overlay_filename, r.raw_filename "
            "FROM recordings r WHERE r.id = ?",
            (recording_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Recording not found")
        if row["user_id"] != user.id and user.role != "admin":
            raise HTTPException(403, "Cannot delete other users' recordings")

        await conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))

    keys_to_delete: list[str] = []
    seen: set[str] = set()
    for fname in (row["filename"], row["overlay_filename"], row["raw_filename"]):
        safe = _safe_filename_for_key(fname)
        if not safe or safe in seen:
            continue
        seen.add(safe)
        keys_to_delete.append(recording_key(int(row["user_id"]), safe))
        stem = PurePosixPath(safe).stem
        if stem:
            keys_to_delete.append(thumbnail_key(int(row["user_id"]), stem))

    if keys_to_delete:
        await delete_many(keys_to_delete)

    _audit("recording.delete", username=user.username, id=recording_id)
    return JSONResponse({"ok": True})


@router.patch("/{recording_id}/share")
async def toggle_share(
    recording_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    _csrf=Depends(check_csrf),
):
    body = await request.json()
    shared = 1 if body.get("shared") else 0
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT user_id FROM recordings WHERE id = ?", (recording_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Recording not found")
        if row["user_id"] != user.id:
            raise HTTPException(403, "Only the owner can share recordings")
        await conn.execute(
            "UPDATE recordings SET shared = ? WHERE id = ?", (shared, recording_id)
        )
    _audit("recording.share", username=user.username, id=recording_id, shared=shared)
    return JSONResponse({"ok": True, "shared": shared})
