from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path

from app.config import settings
from app.database import get_db

log = logging.getLogger("sentinelCam.thumbnails")

_PENDING_RECORDINGS: set[int] = set()
_TASKS: set[asyncio.Task[None]] = set()
_INFLIGHT: dict[int, asyncio.Future[Path]] = {}
_STATS: dict[str, object] = {
    "completed_count": 0,
    "failed_count": 0,
    "last_success_at": None,
    "last_failure_at": None,
    "last_error": "",
    "last_recording_id": None,
}


def _consume_future_exception(future: asyncio.Future[Path]) -> None:
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


def _thumbnail_source_and_path(row: Mapping[str, object]) -> tuple[Path, Path, str]:
    user_id = int(row["user_id"])
    filename = row["overlay_filename"] or row["filename"]
    if not filename:
        raise FileNotFoundError("Recording file not found")

    rec_dir = Path(settings.recordings_path) / str(user_id)
    src_path = rec_dir / str(filename)
    thumb_path = rec_dir / f"thumb_{src_path.stem}.jpg"
    media_type = str(row["type"] or "")
    return src_path, thumb_path, media_type


# Cap PIL decoding to defend against decompression-bomb uploads. ~64 MP is
# more than enough for any reasonable camera frame.
_PIL_MAX_PIXELS = 64 * 1024 * 1024


def _generate_thumbnail_sync(src_path: Path, thumb_path: Path, media_type: str) -> None:
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = thumb_path.with_name(f"{thumb_path.stem}.{uuid.uuid4().hex}.tmp")

    try:
        if media_type == "video":
            import cv2

            cap = cv2.VideoCapture(str(src_path))
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
                resized = cv2.resize(frame, (max(1, int(width * scale)), max(1, int(height * scale))))
            ok, encoded = cv2.imencode(".jpg", resized, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if not ok:
                raise RuntimeError("Could not encode video thumbnail")
            tmp_path.write_bytes(encoded.tobytes())
        else:
            from PIL import Image

            prev_limit = Image.MAX_IMAGE_PIXELS
            Image.MAX_IMAGE_PIXELS = _PIL_MAX_PIXELS
            try:
                with Image.open(src_path) as img:
                    img.load()
                    if img.mode not in ("RGB", "L"):
                        rgba = img.convert("RGBA")
                        background = Image.new("RGB", rgba.size, (18, 22, 26))
                        background.paste(rgba, mask=rgba.getchannel("A"))
                        img = background
                    img.thumbnail((200, 200))
                    img.save(str(tmp_path), "JPEG", quality=85)
            finally:
                Image.MAX_IMAGE_PIXELS = prev_limit

        tmp_path.replace(thumb_path)
    finally:
        tmp_path.unlink(missing_ok=True)


async def ensure_thumbnail_from_row(row: Mapping[str, object]) -> Path:
    src_path, thumb_path, media_type = _thumbnail_source_and_path(row)
    if not src_path.exists():
        raise FileNotFoundError("File not found")
    if thumb_path.exists():
        return thumb_path

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
            if thumb_path.exists():
                if not inflight.done():
                    inflight.set_result(thumb_path)
                return thumb_path
            await asyncio.to_thread(_generate_thumbnail_sync, src_path, thumb_path, media_type)
            if not inflight.done():
                inflight.set_result(thumb_path)
            return thumb_path
        except Exception as exc:
            if not inflight.done():
                inflight.set_exception(exc)
            raise
        finally:
            _INFLIGHT.pop(recording_id, None)

    await asyncio.to_thread(_generate_thumbnail_sync, src_path, thumb_path, media_type)
    return thumb_path


async def ensure_thumbnail(recording_id: int) -> Path:
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
