from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, Response

from app.auth.dependencies import User, check_csrf, get_current_user
from app.config import settings
from app.database import get_db

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


def _get_recordings_dir(user_id: int) -> Path:
    p = Path(settings.recordings_path) / str(user_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


async def _check_quota(user_id: int, add_bytes: int) -> None:
    quota_bytes = settings.storage_quota_per_user_mb * 1024 * 1024
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM recordings WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        used = row[0] if row else 0
    if used + add_bytes > quota_bytes:
        raise HTTPException(413, f"Storage quota exceeded ({settings.storage_quota_per_user_mb} MB limit)")


@router.post("/upload", status_code=201)
async def upload_recording(
    request: Request,
    type: str = Form(...),
    overlay_file: UploadFile = File(...),
    raw_file: Optional[UploadFile] = File(None),
    duration: Optional[float] = Form(None),
    user: User = Depends(get_current_user),
    _csrf=Depends(check_csrf),
):
    if type not in ("image", "video"):
        raise HTTPException(400, "type must be image or video")

    max_bytes = settings.max_upload_size_mb * 1024 * 1024

    # Read overlay file
    overlay_data = await overlay_file.read(max_bytes + 1)
    if len(overlay_data) > max_bytes:
        raise HTTPException(413, f"File too large (max {settings.max_upload_size_mb} MB)")

    # Determine MIME type
    overlay_mime = overlay_file.content_type or ""
    if type == "image" and overlay_mime not in ALLOWED_MIME_IMAGE:
        overlay_mime = "image/jpeg"  # default fallback
    if type == "video" and overlay_mime not in ALLOWED_MIME_VIDEO:
        overlay_mime = "video/webm"

    if not _check_magic_bytes(overlay_data, overlay_mime):
        raise HTTPException(400, "File content does not match expected type")

    await _check_quota(user.id, len(overlay_data))

    rec_dir = _get_recordings_dir(user.id)
    file_uuid = str(uuid.uuid4()).replace("-", "")
    ext = EXTENSION_MAP.get(overlay_mime, "bin")
    overlay_filename = f"{file_uuid}.{ext}"
    overlay_path = rec_dir / overlay_filename
    overlay_path.write_bytes(overlay_data)

    raw_filename = None
    if raw_file and type == "image":
        raw_data = await raw_file.read(max_bytes + 1)
        if len(raw_data) <= max_bytes:
            raw_mime = raw_file.content_type or "image/jpeg"
            if raw_mime in ALLOWED_MIME_IMAGE and _check_magic_bytes(raw_data, raw_mime):
                raw_uuid = str(uuid.uuid4()).replace("-", "")
                raw_ext = EXTENSION_MAP.get(raw_mime, "jpg")
                raw_filename = f"{raw_uuid}_raw.{raw_ext}"
                raw_path = rec_dir / raw_filename
                raw_path.write_bytes(raw_data)

    async with get_db() as conn:
        cursor = await conn.execute(
            "INSERT INTO recordings (user_id, type, filename, overlay_filename, raw_filename, size_bytes, duration_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (
                user.id,
                type,
                overlay_filename,
                overlay_filename,
                raw_filename,
                len(overlay_data),
                duration if type == "video" else None,
            ),
        )
        new_row = await cursor.fetchone()
        await conn.commit()

    _audit("recording.upload", username=user.username, type=type, id=new_row["id"])
    return JSONResponse({"ok": True, "id": new_row["id"]}, status_code=201)


@router.get("")
async def list_recordings(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    type: Optional[str] = None,
    sort: str = "newest",
    user: User = Depends(get_current_user),
):
    if page < 1:
        page = 1
    if per_page < 1 or per_page > 100:
        per_page = 20
    offset = (page - 1) * per_page
    order = "DESC" if sort != "oldest" else "ASC"

    conditions = []
    params: list = []

    if user.role != "admin":
        conditions.append("r.user_id = ?")
        params.append(user.id)

    if type in ("image", "video"):
        conditions.append("r.type = ?")
        params.append(type)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    params_count = list(params)
    params.extend([per_page, offset])

    async with get_db() as conn:
        cursor = await conn.execute(
            f"SELECT COUNT(*) FROM recordings r {where}",
            params_count,
        )
        total_row = await cursor.fetchone()
        total = total_row[0] if total_row else 0

        cursor = await conn.execute(
            f"SELECT r.id, r.type, r.filename, r.overlay_filename, r.raw_filename, "
            f"r.size_bytes, r.duration_seconds, r.created_at, u.username "
            f"FROM recordings r JOIN users u ON r.user_id = u.id {where} "
            f"ORDER BY r.created_at {order} LIMIT ? OFFSET ?",
            params,
        )
        rows = await cursor.fetchall()

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
    return JSONResponse(dict(row))


@router.get("/{recording_id}/file")
async def serve_recording_file(
    recording_id: int,
    variant: Optional[str] = None,
    user: User = Depends(get_current_user),
):
    async with get_db() as conn:
        if user.role == "admin":
            cursor = await conn.execute(
                "SELECT r.user_id, r.filename, r.overlay_filename, r.raw_filename, r.type "
                "FROM recordings r WHERE r.id = ?",
                (recording_id,),
            )
        else:
            cursor = await conn.execute(
                "SELECT r.user_id, r.filename, r.overlay_filename, r.raw_filename, r.type "
                "FROM recordings r WHERE r.id = ? AND r.user_id = ?",
                (recording_id, user.id),
            )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Recording not found")

    if variant == "raw" and row["raw_filename"]:
        fname = row["raw_filename"]
    else:
        fname = row["overlay_filename"] or row["filename"]

    path = Path(settings.recordings_path) / str(row["user_id"]) / fname
    if not path.exists():
        raise HTTPException(404, "File not found on disk")

    ext = path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webm": "video/webm",
        ".mp4": "video/mp4",
    }
    media_type = media_types.get(ext, "application/octet-stream")
    return FileResponse(str(path), media_type=media_type)


@router.get("/{recording_id}/thumbnail")
async def serve_thumbnail(recording_id: int, user: User = Depends(get_current_user)):
    async with get_db() as conn:
        if user.role == "admin":
            cursor = await conn.execute(
                "SELECT r.user_id, r.filename, r.overlay_filename, r.type FROM recordings r WHERE r.id = ?",
                (recording_id,),
            )
        else:
            cursor = await conn.execute(
                "SELECT r.user_id, r.filename, r.overlay_filename, r.type FROM recordings r "
                "WHERE r.id = ? AND r.user_id = ?",
                (recording_id, user.id),
            )
        row = await cursor.fetchone()
    if not row:
        raise HTTPException(404, "Recording not found")

    fname = row["overlay_filename"] or row["filename"]
    rec_dir = Path(settings.recordings_path) / str(row["user_id"])
    src_path = rec_dir / fname
    thumb_path = rec_dir / f"thumb_{fname.rsplit('.', 1)[0]}.jpg"

    if not src_path.exists():
        raise HTTPException(404, "File not found")

    if not thumb_path.exists():
        try:
            from PIL import Image
            with Image.open(src_path) as img:
                img.thumbnail((200, 200))
                img.save(str(thumb_path), "JPEG", quality=85)
        except Exception as e:
            raise HTTPException(500, f"Thumbnail generation failed: {e}")

    return FileResponse(str(thumb_path), media_type="image/jpeg")


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
        await conn.commit()

    rec_dir = Path(settings.recordings_path) / str(row["user_id"])
    for fname in (row["filename"], row["overlay_filename"], row["raw_filename"]):
        if fname:
            p = rec_dir / fname
            if p.exists():
                p.unlink(missing_ok=True)
            # Also remove thumbnail
            thumb = rec_dir / f"thumb_{fname.rsplit('.', 1)[0]}.jpg"
            if thumb.exists():
                thumb.unlink(missing_ok=True)

    _audit("recording.delete", username=user.username, id=recording_id)
    return JSONResponse({"ok": True})
