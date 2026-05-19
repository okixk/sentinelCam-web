from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Optional

from app.database import get_db
from app.storage import (
    delete_object,
    get_bytes,
    object_exists,
    put_bytes,
    recording_key,
    thumbnail_key,
)

log = logging.getLogger("sentinelCam.thumbnails")

_PENDING_RECORDINGS: set[int] = set()
_TASKS: set[asyncio.Task[None]] = set()
_INFLIGHT: dict[int, asyncio.Future[str]] = {}
_STATS: dict[str, object] = {
    "completed_count": 0,
    "failed_count": 0,
    "last_success_at": None,
    "last_failure_at": None,
    "last_error": "",
    "last_recording_id": None,
}


def _consume_future_exception(future: asyncio.Future[str]) -> None:
    if future.cancelled():
        return
    try:
        future.exception()
    except Exception:
        pass


def _mark_thumbnail_success(recording_id: int) -> None:
    _STATS["completed_count"] = int(_STATS["completed_count"] or 0) + 1
    _STATS["last_success_at"] = time.time()
    _STATS["last_error"] = ""
    _STATS["last_recording_id"] = recording_id


def _mark_thumbnail_failure(recording_id: int, error: Exception) -> None:
    _STATS["failed_count"] = int(_STATS["failed_count"] or 0) + 1
    _STATS["last_failure_at"] = time.time()
    _STATS["last_error"] = str(error)
    _STATS["last_recording_id"] = recording_id


def _safe_basename(filename: str | None) -> str | None:
    if not filename:
        return None
    s = str(filename)
    if "/" in s or "\\" in s or ".." in s:
        return None
    return s


def _thumbnail_source_and_keys(row: Mapping[str, object]) -> tuple[str, str, str]:
    user_id = int(row["user_id"])
    filename_raw = row["overlay_filename"] or row["filename"]
    filename = _safe_basename(filename_raw)
    if not filename:
        raise FileNotFoundError("Recording file not found")

    src_key = recording_key(user_id, filename)
    stem = PurePosixPath(filename).stem
    thumb_key_value = thumbnail_key(user_id, stem)
    media_type = str(row["type"] or "")
    return src_key, thumb_key_value, media_type


_PIL_MAX_PIXELS = 64 * 1024 * 1024


def _make_image_thumbnail_bytes(src_bytes: bytes) -> bytes:
    from io import BytesIO

    from PIL import Image

    prev_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = _PIL_MAX_PIXELS
    try:
        with Image.open(BytesIO(src_bytes)) as img:
            img.load()
            if img.mode not in ("RGB", "L"):
                rgba = img.convert("RGBA")
                background = Image.new("RGB", rgba.size, (18, 22, 26))
                background.paste(rgba, mask=rgba.getchannel("A"))
                img = background
            img.thumbnail((200, 200))
            buf = BytesIO()
            img.save(buf, "JPEG", quality=85)
            return buf.getvalue()
    finally:
        Image.MAX_IMAGE_PIXELS = prev_limit


def _make_video_thumbnail_bytes(src_bytes: bytes, suffix: str) -> bytes:
    import cv2

    suffix = suffix if suffix.startswith(".") else f".{suffix or 'tmp'}"
    tmp = tempfile.NamedTemporaryFile(prefix="thumb_", suffix=suffix, delete=False)
    try:
        tmp.write(src_bytes)
        tmp.flush()
        tmp.close()
        cap = cv2.VideoCapture(tmp.name)
        try:
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok or frame is None:
            raise RuntimeError("Could not decode video frame")
        resized = frame
        height, width = frame.shape[:2]
        max_dim = max(width, height)
        if max_dim > 200:
            scale = 200.0 / max_dim
            resized = cv2.resize(
                frame,
                (max(1, int(width * scale)), max(1, int(height * scale))),
            )
        ok, encoded = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError("Could not encode video thumbnail")
        return encoded.tobytes()
    finally:
        Path(tmp.name).unlink(missing_ok=True)


async def _build_and_upload_thumbnail(src_key: str, thumb_key_value: str, media_type: str) -> str:
    try:
        src_bytes = await get_bytes(src_key)
    except FileNotFoundError:
        raise

    if media_type == "video":
        suffix = PurePosixPath(src_key).suffix or ".bin"
        thumb_bytes = await asyncio.to_thread(_make_video_thumbnail_bytes, src_bytes, suffix)
    else:
        thumb_bytes = await asyncio.to_thread(_make_image_thumbnail_bytes, src_bytes)

    await put_bytes(thumb_key_value, thumb_bytes, content_type="image/jpeg")
    return thumb_key_value


async def ensure_thumbnail_from_row(row: Mapping[str, object]) -> str:
    src_key, thumb_key_value, media_type = _thumbnail_source_and_keys(row)
    if not await object_exists(src_key):
        raise FileNotFoundError("Recording file not found")
    if await object_exists(thumb_key_value):
        return thumb_key_value

    recording_id_value = row["id"] if "id" in row.keys() else None
    recording_id = int(recording_id_value) if recording_id_value is not None else None
    if recording_id is not None:
        inflight = _INFLIGHT.get(recording_id)
        if inflight is not None:
            return await asyncio.shield(inflight)

        loop = asyncio.get_running_loop()
        inflight = loop.create_future()
        inflight.add_done_callback(_consume_future_exception)
        _INFLIGHT[recording_id] = inflight
        try:
            if await object_exists(thumb_key_value):
                if not inflight.done():
                    inflight.set_result(thumb_key_value)
                return thumb_key_value
            result = await _build_and_upload_thumbnail(src_key, thumb_key_value, media_type)
            if not inflight.done():
                inflight.set_result(result)
            return result
        except Exception as exc:
            if not inflight.done():
                inflight.set_exception(exc)
            raise
        finally:
            _INFLIGHT.pop(recording_id, None)

    return await _build_and_upload_thumbnail(src_key, thumb_key_value, media_type)


async def ensure_thumbnail(recording_id: int) -> str:
    async with get_db() as conn:
        cursor = await conn.execute(
            "SELECT r.id, r.user_id, r.filename, r.overlay_filename, r.type FROM recordings r WHERE r.id = ?",
            (recording_id,),
        )
        row = await cursor.fetchone()
    if not row:
        raise FileNotFoundError("Recording not found")
    return await ensure_thumbnail_from_row(row)


async def _warm_recordings(recording_ids: list[int]) -> None:
    try:
        async with get_db() as conn:
            for recording_id in recording_ids:
                try:
                    cursor = await conn.execute(
                        "SELECT r.id, r.user_id, r.filename, r.overlay_filename, r.type FROM recordings r WHERE r.id = ?",
                        (recording_id,),
                    )
                    row = await cursor.fetchone()
                    if not row:
                        continue
                    await ensure_thumbnail_from_row(row)
                    _mark_thumbnail_success(recording_id)
                except FileNotFoundError:
                    log.debug("Thumbnail warm-up skipped for missing recording source %s", recording_id)
                except Exception as exc:
                    _mark_thumbnail_failure(recording_id, exc)
                    log.exception("Thumbnail warm-up failed for recording %s", recording_id)
    finally:
        for recording_id in recording_ids:
            _PENDING_RECORDINGS.discard(recording_id)


def schedule_thumbnail_warmup(recording_ids: Iterable[int]) -> None:
    ids: list[int] = []
    seen: set[int] = set()
    for recording_id in recording_ids:
        try:
            rid = int(recording_id)
        except (TypeError, ValueError):
            continue
        if rid <= 0 or rid in seen or rid in _PENDING_RECORDINGS or rid in _INFLIGHT:
            continue
        seen.add(rid)
        _PENDING_RECORDINGS.add(rid)
        ids.append(rid)

    if not ids:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        for rid in ids:
            _PENDING_RECORDINGS.discard(rid)
        return

    task = loop.create_task(_warm_recordings(ids))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def get_thumbnail_job_stats() -> dict[str, object]:
    return {
        "pending_count": len(_PENDING_RECORDINGS),
        "inflight_count": len(_INFLIGHT),
        "active_tasks": sum(1 for task in _TASKS if not task.done()),
        "completed_count": int(_STATS["completed_count"] or 0),
        "failed_count": int(_STATS["failed_count"] or 0),
        "last_success_at": _STATS["last_success_at"],
        "last_failure_at": _STATS["last_failure_at"],
        "last_error": str(_STATS["last_error"] or ""),
        "last_recording_id": _STATS["last_recording_id"],
    }


def reset_thumbnail_job_stats() -> None:
    _PENDING_RECORDINGS.clear()
    _TASKS.clear()
    _INFLIGHT.clear()
    _STATS.update(
        {
            "completed_count": 0,
            "failed_count": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_error": "",
            "last_recording_id": None,
        }
    )


async def shutdown_thumbnail_jobs() -> None:
    tasks = list(_TASKS)
    _TASKS.clear()
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=2.0)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.exception()
    for future in list(_INFLIGHT.values()):
        if not future.done():
            future.cancel()
    _INFLIGHT.clear()
    _PENDING_RECORDINGS.clear()
